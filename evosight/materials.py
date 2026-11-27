"""Search publisher sites, poll their public resources and retain locatable evidence."""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

from lxml import etree, html

from . import feeds, market_sources
from .db import later, one, rows, utc


@dataclass(frozen=True)
class Source:
    url: str
    kind: str
    name: str
    reason: str
    origin: str


@dataclass
class Extract:
    kind: str
    parts: list[tuple[str, str, str]] = field(default_factory=list)
    attachments: list[tuple[str, str]] = field(default_factory=list)
    title: str = ""
    published_at: str | None = None
    detail: str = ""
    playback: dict[str, str] = field(default_factory=dict)


def safe_link(base, value):
    try:
        link = urljoin(base, value or "")
        if link.startswith("http://"):
            link = "https://" + link[7:]
        return feeds.canonical(feeds.public_url(link))
    except (ValueError, UnicodeError):
        return ""


def words(text):
    """Keep proper names and Chinese bigrams for short-title matching."""
    value = text.casefold()
    tokens = set(re.findall(r"[a-z][a-z0-9.+-]{1,35}", value))
    for group in re.findall(r"[\u3400-\u9fff]{2,}", value):
        tokens.update(group[i : i + 2] for i in range(len(group) - 1))
    return tokens - {
        "事件",
        "跟踪",
        "发布",
        "是否",
        "官方",
        "研究",
        "观察",
        "关注",
        "最新",
        "项目",
        "情况",
        "公司",
        "新闻",
        "信息",
        "进展",
        "the",
        "and",
    }


def relevance(title, scope, headline):
    terms = words(headline)
    return 3 * len(terms & words(title)) + len(terms & words(scope))


CATALOG = (
    (("vue", "vuejs"), "https://github.com/vuejs/core/releases.atom", "Vue 官方发布", "项目团队直接公布发布版本"),
    (
        ("苹果", "apple", "iphone", "macbook"),
        "https://www.apple.com/newsroom/rss-feed.rss",
        "Apple Newsroom",
        "公司直接披露产品和经营动态",
    ),
    (
        ("统计", "经济数据", "cpi", "居民消费价格", "工业增加值"),
        "https://www.stats.gov.cn/sj/zxfb/rss.xml",
        "国家统计局",
        "主管机关首次公布统计数据",
    ),
    (
        ("公共", "社会", "国内", "城市", "交通", "防汛", "天气", "应急", "地铁"),
        "https://www.chinanews.com.cn/rss/society.xml",
        "中新网社会新闻",
        "新闻快讯覆盖国内公共事件",
    ),
    (
        ("科技", "产业", "半导体", "芯片", "人工智能", "公司", "苹果"),
        "https://www.chinanews.com.cn/rss/it.xml",
        "中新网科技新闻",
        "行业新闻可补充企业披露",
    ),
)


def catalog(title, scope):
    text = (title + " " + scope).casefold()
    found = [
        Source(url, "feed", name, reason, "目录匹配")
        for terms, url, name, reason in CATALOG
        if any(term in text for term in terms)
    ]
    if re.search(r"(?<![a-z0-9])anthropic(?![a-z0-9])", text):
        if re.search(r"股票|股价|涨跌|交易|行情|证券|上市|投资|融资|财报|股市|stock|shares?|ipo|earnings|trading", text):
            found.extend(
                [
                    Source(
                        "https://stockanalysis.com/stocks/ant/history/",
                        "daily_prices",
                        "Anthropic (NASDAQ: ANT) 历史日线",
                        "按交易日核对证券代码、币种与收盘记录",
                        "目录匹配",
                    ),
                    Source(
                        "https://data.sec.gov/submissions/CIK0002130357.json",
                        "sec_filings",
                        "SEC · Anthropic 申报索引",
                        "主管机关直接记录发行人、表单与受理时间",
                        "目录匹配",
                    ),
                    Source(
                        "https://finance.yahoo.com/quote/ANT/history/",
                        "daily_prices",
                        "Yahoo Finance · Anthropic 历史日线",
                        "按日期交叉核对独立发布的交易行情",
                        "目录匹配",
                    ),
                    Source(
                        "https://www.anthropic.com/news/ipo-close",
                        "page",
                        "Anthropic 上市公告",
                        "发行人直接确认挂牌交易所及代码",
                        "目录匹配",
                    ),
                ]
            )
        found.append(
            Source("https://www.anthropic.com/news", "section", "Anthropic 官方新闻", "发行人公告栏目，可持续回访", "目录匹配")
        )
    return found


def search_web(query, maximum=4):
    from ddgs import DDGS
    from ddgs.exceptions import DDGSException

    try:
        results = DDGS(timeout=8).text(query[:320], max_results=maximum, backend="auto")
    except DDGSException as exc:
        if str(exc) == "No results found.":
            return []
        raise RuntimeError("公开搜索服务暂不可用") from exc
    return [(r.get("title", ""), r.get("href") or r.get("url", "")) for r in results]


def tree_for(resource):
    charset = re.search(r"charset\s*=\s*[\"']?([\w.-]+)", resource.mime, re.I)
    encoding = charset.group(1) if charset else None
    if not encoding:
        try:
            resource.body.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            pass
    parser = html.HTMLParser(encoding=encoding, no_network=True)
    return html.fromstring(resource.body, parser=parser, base_url=resource.url)


def clean_text(value, limit=3500):
    return " ".join(str(value or "").split())[:limit]


def article(resource):
    import trafilatura

    root = tree_for(resource)
    url = resource.url
    visible_h1 = root.xpath(
        "(//h1)[1]//text()[not(ancestor::script or ancestor::style or ancestor::noscript)]"
    )
    title = clean_text(" ".join(visible_h1), 400) or clean_text(
        " ".join(
            root.xpath('//meta[@property="og:title" or @name="twitter:title"]/@content')[:1]
            or root.xpath("(//title)[1]/text()")
        ),
        400,
    )
    published = next(
        (
            stamp
            for raw in root.xpath(
                '//meta[@property="article:published_time" or @itemprop="datePublished"]/@content | //time[@itemprop="datePublished"]/@datetime'
            )
            if (stamp := feeds.timestamp(raw))
        ),
        None,
    )
    for raw in root.xpath('//script[@type="application/ld+json"]/text()')[:12]:
        try:
            objects = json.loads(raw)
            objects = objects if isinstance(objects, list) else [objects]
            for obj in objects:
                for item in [obj, *(obj.get("@graph", []) if isinstance(obj, dict) else [])]:
                    if isinstance(item, dict) and item.get("datePublished"):
                        published = published or feeds.timestamp(str(item["datePublished"]))
        except (TypeError, ValueError):
            continue
    result = Extract("html", title=title, published_at=published)
    xml = trafilatura.extract(
        resource.body,
        url=url,
        output_format="xml",
        include_tables=True,
        include_comments=False,
        include_links=False,
    )
    paragraphs = []
    if xml:
        parsed = etree.fromstring(
            xml.encode(), parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
        paragraphs = [
            clean_text(" ".join(e.itertext())) for e in parsed.xpath("//main//p | //main//item | //main//row")
        ]
    if not paragraphs:
        paragraphs = [clean_text(p.text_content()) for p in root.xpath("//article//p | //main//p")]
    # On a personal profile the heading supplies the name for a first-person public biography.
    # Keep both source spans together so a role can cite the actual name and office in one fragment.
    if re.fullmatch(r"[A-Za-zÀ-ž][\wÀ-ž.’'-]*(?: [A-Za-zÀ-ž][\wÀ-ž.’'-]*){1,3}|[\u3400-\u9fff·]{2,7}", title):
        identities = [
            p
            for p in paragraphs
            if re.search(
                r"\bI (?:am|was) (?:the |a |an )?(?:founder|creator|maintainer|director|president|spokesperson)|我(?:是|担任).{0,24}(?:创始人|负责人|发言人|维护者)",
                p,
                re.I,
            )
        ]
        for p in identities[:2]:
            result.parts.append(("页面标题与本人公开身份", title + " — " + p, url.split("#", 1)[0]))
    for i, p in enumerate(dict.fromkeys(s for s in paragraphs if s), 1):
        result.parts.append((f"正文第 {i} 段", p, url.split("#", 1)[0] + "#:~:text=" + quote(p[:72], safe="")))
        if len(result.parts) >= 120:
            break
    media = root.xpath("//article | //main") or [root]
    for container in media[:1]:
        for elem in container.xpath(
            ".//track[@src] | .//a[@href] | .//figure//img[@src] | .//video[@src] | .//audio[@src] | .//video/source[@src] | .//audio/source[@src]"
        ):
            link = safe_link(url, elem.get("src") or elem.get("href"))
            if not link:
                continue
            path = urlsplit(link).path.lower()
            if elem.tag == "track" or path.endswith((".vtt", ".srt")):
                kind = "captions"
                parent = elem.xpath("ancestor::video[1] | ancestor::audio[1]")
                if parent:
                    playable = parent[0].get("src") or next(iter(parent[0].xpath("./source/@src")), "")
                    if playable:
                        result.playback[link] = safe_link(url, playable)
            elif path.endswith(".pdf"):
                kind = "pdf"
            elif elem.tag == "img":
                kind = "image"
            elif elem.tag in ("video", "audio", "source"):
                kind = "media"
            else:
                continue
            if (kind, link) not in result.attachments:
                result.attachments.append((kind, link))
    if not result.parts:
        result.detail = "未能定位网页正文，保留订阅材料"
    return result


def caption_parts(body, url):
    text = body.decode("utf-8-sig", errors="replace").replace("\r", "")
    result = []
    for block in re.split(r"\n\s*\n", text):
        match = re.search(r"(?m)^([\d:]+[.,]\d+)\s*-->\s*([\d:]+[.,]\d+)[^\n]*\n(.+)", block, re.S)
        if not match:
            continue

        def seconds(t):
            total = 0.0
            for part in t.replace(",", ".").split(":"):
                total = total * 60 + float(part)
            return total

        a, b, raw = match.groups()
        value = feeds.clean(raw, 1500)
        if value:
            result.append((f"字幕 {a}–{b}", value, f"{url}#t={seconds(a):g},{seconds(b):g}"))
    return result[:300]


def extract(resource, kind="auto", playback=""):
    path = urlsplit(resource.url).path.casefold()
    if kind == "pdf" or resource.body.startswith(b"%PDF"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(resource.body))
        result = Extract("pdf")
        scanned = []
        for i, page in enumerate(reader.pages[:40], 1):
            value = clean_text(page.extract_text(), 6500)
            if value:
                result.parts.append((f"第 {i} 页", value, resource.url + f"#page={i}"))
            else:
                scanned.append(i)
        if scanned:
            from .media import scanned_pages

            result.parts.extend(scanned_pages(resource, scanned))
            result.detail = "扫描页按需识别，未取得可靠文字的区域待核"
        return result
    if kind == "captions" or path.endswith((".vtt", ".srt")):
        return Extract("captions", caption_parts(resource.body, playback or resource.url))
    if kind in ("image", "media") or resource.mime.startswith(("image/", "audio/", "video/")):
        from .media import extract_media

        return extract_media(resource, kind)
    return article(resource)


def section_entries(resource):
    root = tree_for(resource)
    found = {}
    for anchor in root.xpath(
        "//main//a[@href] | //article//a[@href] | //li//a[@href] | //h2/a[@href] | //h3/a[@href]"
    ):
        link = safe_link(resource.url, anchor.get("href"))
        name = clean_text(anchor.get("title") or anchor.text_content(), 400)
        if (
            len(name) < 8
            or not link
            or link == resource.url
            or urlsplit(link).hostname != urlsplit(resource.url).hostname
        ):
            continue
        if re.search(r"\.(?:js|css|png|jpg|zip)$", urlsplit(link).path, re.I):
            continue
        found.setdefault(
            link, {"url": link, "title": name, "summary": "", "published_at": None, "listing_only": True}
        )
        if len(found) >= 60:
            break
    return list(found.values())


class Acquisition:
    def __init__(self, *, download=None, search=None):
        self.download = download or feeds.download
        self.search = search or search_web

    def discover(self, title, scope, known, *, force_search=False, query=None):
        choices = list(known) + ([] if query else catalog(title, scope))
        attempts = []
        # A known primary source is enough to start. A missing or secondary source needs a publisher search.
        if (
            query
            or force_search
            or not choices
            or not any(s.origin == "用户指定" or "直接" in s.reason or "主管" in s.reason for s in choices)
        ):
            query = query or f"{title} {scope[:65]} 官方 公告"
            try:
                hits = self.search(query, 4)
                attempts.append((query, f"检索得到 {len(hits)} 个候选"))
            except Exception:
                hits = []
                attempts.append((query, "搜索服务暂不可用"))
            for name, raw in hits:
                url = safe_link("", raw)
                if not url or not relevance(title, scope, name + " " + url):
                    continue
                try:
                    res = self.download(url)
                    if "xml" in res.mime and feeds.parse_feed(res.body, res.url, res.mime):
                        choices.append(Source(res.url, "feed", name, "相关检索结果已验证为发布方订阅", "搜索发现"))
                        continue
                    root = tree_for(res)
                    alts = root.xpath(
                        '//link[contains(@rel,"alternate") and (contains(@type,"rss") or contains(@type,"atom"))]/@href'
                    )
                    repo = re.match(r"https://github\.com/([\w.-]+/[\w.-]+)(?:/|$)", url)
                    if repo:
                        alts.insert(0, f"https://github.com/{repo[1]}/releases.atom")
                    if alts and (feed := safe_link(res.url, alts[0])):
                        choices.append(Source(feed, "feed", name, "发布方页面公布的订阅入口", "搜索发现"))
                        continue
                    listing = bool(
                        root.xpath("//time | //article//h1 | //meta[@property='article:published_time']")
                    )
                    kind = "page" if listing or not section_entries(res) else "section"
                    choices.append(Source(res.url, kind, name, "回访与研究目标相关的发布方页面", "搜索发现"))
                except Exception:
                    attempts.append((url, "回访失败，未能核实来源"))
        unique = {}
        for choice in choices:
            unique.setdefault(choice.url, choice)
        return list(unique.values())[:8], attempts

    def poll(self, source, cached=None):
        res = self.download(source.url, cached)
        if res.unchanged:
            entries = []
        elif source.kind in ("sec_filings", "daily_prices"):
            if market_sources.source_kind(res.url) != source.kind:
                raise ValueError("来源重定向后的发布方或数据类型不符")
            entries = (
                market_sources.filings(res)
                if source.kind == "sec_filings"
                else market_sources.daily_prices(res, tree_for(res))
            )
            if "anthropic" in source.name.casefold() and not re.search(
                r"\banthropic\b", entries[0]["title"], re.I
            ):
                raise ValueError("发布记录中的发行人与指定来源不符")
            return feeds.FeedPage(entries, res.url, res.etag, res.modified, original=res)
        elif source.kind == "feed":
            entries = feeds.parse_feed(res.body, res.url, res.mime)
        elif source.kind == "section":
            entries = section_entries(res)
        else:
            content = extract(res)
            if not content.parts:
                raise ValueError("发布页没有可定位的内容")
            entries = [
                {
                    "url": res.url,
                    "title": content.title or source.name,
                    "summary": " ".join(p[1] for p in content.parts)[:6000],
                    "published_at": content.published_at,
                }
            ]
        return feeds.FeedPage(entries, res.url, res.etag, res.modified, res.unchanged)

    def scout_sources(self, title, scope, plan):
        choices, attempts = {}, []
        for raw in dict.fromkeys(plan.publisher_urls):
            url = safe_link("", raw)
            if not url:
                continue
            try:
                page = self.download(url)
                kind = market_sources.source_kind(page.url)
                if kind == "sec_filings":
                    records = market_sources.filings(page)
                elif kind == "daily_prices":
                    records = market_sources.daily_prices(page, tree_for(page))
                elif "xml" in page.mime:
                    records = feeds.parse_feed(page.body, page.url, page.mime)
                    kind = "feed"
                else:
                    root = tree_for(page)
                    entries = section_entries(page)
                    body = article(page)
                    heading = clean_text(root.xpath("string(//title)"), 250)
                    records = [
                        {
                            "title": heading + " " + body.title,
                            "summary": " ".join(p[1] for p in body.parts)[:1400],
                        },
                        *entries[:8],
                    ]
                    path = urlsplit(page.url).path.rstrip("/").casefold()
                    kind = (
                        "section"
                        if len(entries) >= 3
                        and (path in ("", "/news", "/newsroom", "/press", "/investors") or not body.parts)
                        else "page"
                    )
                actual = " ".join(e["title"] + " " + e.get("summary", "")[:160] for e in records[:9])
                title_names = {
                    t
                    for t in words(title)
                    if t.isascii()
                    and t not in {"stock", "shares", "tracking", "news", "price", "market", "update"}
                }
                if (
                    not records
                    or not relevance(title, scope, actual)
                    or (title_names and title_names.isdisjoint(words(actual)))
                ):
                    attempts.append((url, "发布方资料与事件主体未匹配"))
                    continue
                name = records[0]["title"][:200]
                choices[page.url] = Source(page.url, kind, name, "探索 Agent 提议，已回访并核对相关内容", "探索补源")
                attempts.append((page.url, "已验证探索 Agent 提议的发布入口"))
            except (OSError, ValueError, RuntimeError, etree.Error) as exc:
                attempts.append((url, str(exc)[:180] if isinstance(exc, ValueError) else "发布方回访失败"))
        if not choices:
            for query in dict.fromkeys(plan.queries):
                extra, logs = self.discover(title, scope, [], query=query)
                attempts.extend(logs)
                for source in extra:
                    choices.setdefault(source.url, source)
                if choices:
                    break
        return list(choices.values()), attempts

    def targeted(self, query):
        results = []
        candidates = [("", query)] if query.lower().startswith(("https://", "http://")) else self.search(query, 4)
        for label, raw in candidates:
            url = safe_link("", raw)
            if not url:
                continue
            try:
                response = self.download(url)
                value = extract(response)
                if value.parts:
                    results.append(
                        (
                            {
                                "url": response.url,
                                "title": value.title or label,
                                "summary": " ".join(x[1] for x in value.parts)[:6000],
                                "published_at": value.published_at,
                            },
                            response,
                            value,
                        )
                    )
            except Exception:
                continue
            if len(results) >= 2:
                break
        return results


def save_asset(c, doc, url, kind, body, mime, result, *, extractor="publisher-v1"):
    digest = hashlib.sha256(body).hexdigest() if body else None
    if digest:
        c.execute("INSERT OR IGNORE INTO evidence_blobs VALUES(?,?,?)", (digest, mime, body))
    existing = one(
        c,
        "SELECT id FROM evidence_assets WHERE document_id=? AND url=? AND extractor=? AND digest IS ?",
        (doc, url, extractor, digest),
    )
    asset = (
        existing["id"]
        if existing
        else c.execute(
            """INSERT INTO evidence_assets(document_id,url,kind,state,detail,digest,extractor,created_at)
                         VALUES(?,?,?,?,?,?,?,?)""",
            (doc, url, kind, "ready" if result.parts else "partial", result.detail, digest, extractor, utc()),
        ).lastrowid
    )
    c.executemany(
        "INSERT OR IGNORE INTO fragments(asset_id,locator,text,link) VALUES(?,?,?,?)",
        [(asset, loc, text, link) for loc, text, link in result.parts if text],
    )
    if result.attachments:
        prioritized = sorted(result.attachments, key=lambda item: item[0] != "captions")
        c.executemany(
            "INSERT OR IGNORE INTO evidence_children(parent_id,url,kind,playback) VALUES(?,?,?,?)",
            [
                (asset, link, child_kind, result.playback.get(link, ""))
                for child_kind, link in prioritized[:4]
            ],
        )
    if (
        not existing
        and digest
        and (kind in ("daily_prices", "sec_filings") or kind == "html" and b"application/ld+json" in body)
    ):
        from . import cards, world_search

        if cards.publisher(c, {"asset_id": asset, "asset_url": url, "kind": kind, "digest": digest})[0]:
            for entry in rows(
                c,
                """SELECT w.id,w.head_id FROM world_entries w JOIN claims k ON k.id=w.head_id
                             WHERE EXISTS(SELECT 1 FROM json_each(k.refs) WHERE value=?)""",
                (doc,),
            ):
                cards.refresh(c, entry["head_id"])
                world_search.index_head(c, entry["id"])
    return asset


def summary_fragment(c, doc):
    text = doc["summary"] or doc["title"]
    return save_asset(
        c,
        doc["id"],
        doc["url"],
        "feed",
        text.encode(),
        "text/plain",
        Extract("feed", [("发布方订阅条目", text, doc["url"])]),
        extractor="feed-v1",
    )


def attach_search_result(c, event, entry, response, ex, intake):
    """An article reached from search and its shorter feed card are the same material."""
    url = feeds.canonical(response.url)
    known = one(c, "SELECT * FROM documents WHERE url=? ORDER BY id DESC LIMIT 1", (url,))
    previous = None
    if known:
        previous = one(
            c,
            """SELECT id FROM evidence_assets WHERE document_id=? AND url=? AND kind=? AND state='ready'
                         ORDER BY id DESC LIMIT 1""",
            (known["id"], url, ex.kind),
        )
    same_rendition = not previous or [
        r[0] for r in c.execute("SELECT text FROM fragments WHERE asset_id=? ORDER BY id", (previous["id"],))
    ] == [p[1] for p in ex.parts]
    if known and same_rendition:
        doc = known["id"]
        c.execute(
            "INSERT OR IGNORE INTO event_docs(event_id,document_id,intake) VALUES(?,?,?)",
            (event, doc, intake),
        )
        if ex.published_at and not known["published_at"]:
            c.execute(
                "UPDATE documents SET published_at=?,source_times_checked=1 WHERE id=?",
                (ex.published_at, doc),
            )
    else:
        doc = feeds.ingest(
            c, event, url, {**entry, "title": known["title"] if known else entry["title"]}, intake=intake
        )
    save_asset(c, doc, url, ex.kind, response.body, response.mime, ex)
    return doc


def enrich(store, acquisition, doc):
    with store.connection(True) as c:
        record = one(
            c,
            "SELECT url FROM evidence_assets WHERE document_id=? AND extractor='publisher-record-v1' AND state='ready' LIMIT 1",
            (doc["id"],),
        )
        if not record:
            summary_fragment(c, doc)
        elif record["url"] == doc["url"]:
            return
    url = safe_link("", doc["url"])
    if not url:
        return
    with store.connection() as c:
        parent = one(
            c,
            """SELECT id FROM evidence_assets WHERE document_id=? AND extractor='publisher-v1'
                   AND (url=? OR kind='html') AND digest IS NOT NULL ORDER BY id LIMIT 1""",
            (doc["id"], url),
        )
    if not parent:
        try:
            response = acquisition.download(url)
        except Exception:
            with store.connection(True) as c:
                detail = "申报正文暂不可取，可核对 SEC 申报索引" if record else "原文暂不可取，保留发布方订阅条目"
                save_asset(c, doc["id"], url, "page", None, "", Extract("page", detail=detail))
            return
        try:
            value = extract(response)
        except Exception:
            value = Extract("page", detail="已保存发布方原件，正文提取待核")
        with store.connection(True) as c:
            parent_id = save_asset(
                c, doc["id"], response.url, value.kind, response.body, response.mime, value
            )
            parent = {"id": parent_id}
            if value.published_at and not doc["published_at"]:
                c.execute(
                    "UPDATE documents SET published_at=?,source_times_checked=1 WHERE id=? AND published_at IS NULL",
                    (value.published_at, doc["id"]),
                )
    with store.connection() as c:
        pending = rows(
            c,
            """SELECT * FROM evidence_children WHERE parent_id=? AND asset_id IS NULL
                   AND (last_attempt IS NULL OR last_attempt<=?) ORDER BY rowid""",
            (parent["id"], later(-1800)),
        )
    for child in pending:
        kind, link = child["kind"], child["url"]
        if kind == "media":
            with store.connection(True) as c:
                caption = one(
                    c,
                    """SELECT 1 FROM evidence_children l JOIN evidence_assets a ON a.id=l.asset_id
                            WHERE l.parent_id=? AND l.playback=? AND l.kind='captions' AND a.state='ready'""",
                    (parent["id"], link),
                )
                if caption:
                    asset = save_asset(
                        c, doc["id"], link, kind, None, "", Extract(kind, detail="本轮使用发布方字幕，时间码指向原媒体")
                    )
                    c.execute(
                        "UPDATE evidence_children SET asset_id=?,last_attempt=? WHERE parent_id=? AND url=?",
                        (asset, utc(), parent["id"], link),
                    )
                    continue
        try:
            attachment = acquisition.download(link)
        except Exception:
            with store.connection(True) as c:
                save_asset(c, doc["id"], link, kind, None, "", Extract(kind, detail="附件待核，可从发布方链接复查"))
                c.execute(
                    "UPDATE evidence_children SET last_attempt=? WHERE parent_id=? AND url=?",
                    (utc(), parent["id"], link),
                )
            continue
        try:
            part = extract(attachment, kind, child["playback"])
        except Exception:
            part = Extract(kind, detail="已留存原件，此附件提取失败")
        with store.connection(True) as c:
            asset = save_asset(c, doc["id"], link, kind, attachment.body, attachment.mime, part)
            c.execute(
                "UPDATE evidence_children SET asset_id=?,last_attempt=? WHERE parent_id=? AND url=?",
                (asset, utc(), parent["id"], link),
            )


def fragments_for(c, doc_ids, *, per_document=12):
    # Round-robin across documents; one long PDF cannot evict all other sources.
    groups = []
    for doc in doc_ids:
        group = rows(
            c,
            """SELECT f.id,a.document_id,f.locator,f.text,f.link,a.kind,d.title,d.url,
                          d.published_at,d.source_updated_at,d.source_times_checked
                          FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id
                          JOIN documents d ON d.id=a.document_id WHERE a.document_id=?
                          AND (a.extractor!='feed-v1' OR NOT EXISTS (SELECT 1 FROM evidence_assets x
                            WHERE x.document_id=a.document_id AND x.extractor='publisher-record-v1' AND x.state='ready'))
                          ORDER BY CASE a.kind WHEN 'feed' THEN 1 ELSE 0 END,f.id LIMIT ?""",
            (doc, per_document if per_document is not None else -1),
        )
        groups.append(group)
    return [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
