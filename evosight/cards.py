"""Provenance of searchable source metadata on individual knowledge versions."""
import json
import re
from urllib.parse import urldefrag, urlsplit

from lxml import etree, html

from .db import dump, one, rows

CARD_FIELDS = {"publisher", "source", "format", "stage"}
FORMATS = {
    "html": ("HTML", "网页", "网页正文"),
    "feed": ("RSS", "RSS摘要", "订阅摘要"),
    "pdf": ("PDF", "PDF正文"),
    "daily_prices": ("行情数据", "交易日行情", "日线"),
    "sec_filings": ("监管申报索引", "SEC申报索引", "SEC文件元数据"),
}
PHASES = {
    "已交割": r"announced the closing|closed the concurrent sale|已(?:经)?(?:完成)?交割",
    "已定价": r"priced (?:its|the) .*?offering|已(?:经)?(?:完成)?定价",
    "定价": r"priced (?:its|the) .*?offering|定价",
    "已生效": r"entered into force|has taken effect|已生效",
    "已通过": r"was adopted|has been approved|已通过",
    "预计交割": r"expected to close|预计.{0,10}交割",
}


def html_publisher(raw, url):
    try:
        root = html.fromstring(raw)
    except (ValueError, etree.ParserError):
        return None
    names = set()
    for snippet in root.xpath('//script[@type="application/ld+json"]/text()')[:24]:
        try:
            items = json.loads(snippet)
        except (TypeError, ValueError, RecursionError):
            continue
        for group in items if isinstance(items, list) else [items]:
            if not isinstance(group, dict):
                continue
            graph = group.get("@graph", [])
            for item in [group, *(graph if isinstance(graph, list) else [])]:
                if not isinstance(item, dict):
                    continue
                types = item.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if not isinstance(types, list) or not any(
                    isinstance(t, str) and t.endswith(("Article", "Posting", "Report")) for t in types
                ):
                    continue
                page = item.get("url") or item.get("mainEntityOfPage")
                if isinstance(page, dict):
                    page = page.get("@id")
                if not isinstance(page, str) or urldefrag(page)[0].rstrip("/") != url.rstrip("/"):
                    continue
                publisher = item.get("publisher")
                name = publisher.get("name") if isinstance(publisher, dict) else publisher
                if isinstance(name, str) and 1 <= len(name.strip()) <= 160:
                    names.add(name.strip())
    return next(iter(names)) if len(names) == 1 else None


def publisher(c, item):
    cached = one(c, "SELECT name,locator FROM archive_publishers WHERE asset_id=?", (item["asset_id"],))
    if cached:
        return cached["name"], cached["locator"]
    name = locator = ""
    host = urlsplit(item["asset_url"]).hostname
    if item["kind"] == "sec_filings" and host == "data.sec.gov":
        name, locator = "U.S. Securities and Exchange Commission", "SEC 申报索引采集器"
    elif item["kind"] == "daily_prices" and host == "stockanalysis.com":
        name, locator = "Stock Analysis", "交易日线采集器"
    elif item["kind"] == "html":
        blob = one(
            c,
            "SELECT content FROM evidence_blobs WHERE digest=? AND length(content)<=8000000",
            (item["digest"],),
        )
        if blob and (value := html_publisher(blob["content"], item["asset_url"])):
            name, locator = value, "JSON-LD article.publisher"
    c.execute("INSERT OR IGNORE INTO archive_publishers VALUES(?,?,?)", (item["asset_id"], name, locator))
    return name, locator


def supported(key, value, source, quote):
    literal = value.casefold() in quote.casefold()
    if key == "source" and value.startswith(("http://", "https://")):
        return urldefrag(value)[0] in (source["url"], source["asset_url"])
    if key == "format":
        formats = {v.casefold() for v in FORMATS.get(source["kind"], ())}
        if value.casefold() in formats:
            return True
        if any(value.casefold() == v.casefold() for names in FORMATS.values() for v in names):
            return False
        return literal and not re.search(r"unavailable|not (?:provided|available)|缺少|尚未取得|未提供", quote, re.I)
    if key == "stage":
        for clause in re.split(r"[。；;]|\.(?:\s+|$)", quote):
            if value.casefold() not in clause.casefold() and not (
                value in PHASES and re.search(PHASES[value], clause, re.I)
            ):
                continue
            if re.search(r"\b(?:not|never|no longer|if|might|may)\b|未|并非|如果", clause, re.I):
                continue
            if value not in ("预计交割",) and re.search(r"expected to|subject to|拟|预计|有待", clause, re.I):
                continue
            return True
        return False
    return literal


def verify(c, citations, values, field_refs):
    evidence = []
    for cite in citations:
        source = one(
            c,
            """SELECT f.asset_id,f.text,a.kind,a.digest,a.url asset_url,d.url FROM fragments f
                        JOIN evidence_assets a ON a.id=f.asset_id JOIN documents d ON d.id=a.document_id
                        WHERE f.id=? AND d.id=?""",
            (cite["fragment_id"], cite["document_id"]),
        )
        if source:
            source.update(cite)
            source["publisher"], source["publisher_locator"] = publisher(c, source)
            source["publisher_asset"] = source["asset_id"]
            source["publisher_url"] = source["asset_url"]
            if not source["publisher"]:
                # A refreshed capture can add the JSON-LD header omitted from an earlier
                # capture. Require the same document and full paragraph. A typed publisher
                # record carries its own checked authority URL.
                copies = rows(
                    c,
                    """SELECT DISTINCT a.id asset_id,a.url asset_url,a.kind,a.digest FROM fragments f
                    JOIN evidence_assets a ON a.id=f.asset_id WHERE a.document_id=? AND
                    ((a.url=? AND a.kind='html') OR a.kind IN ('daily_prices','sec_filings')) AND f.text=?
                    AND a.id!=? ORDER BY a.id""",
                    (cite["document_id"], source["asset_url"], source["text"], source["asset_id"]),
                )
                identified = []
                for copy in copies:
                    name, locator = publisher(c, copy)
                    if name:
                        identified.append((name, locator, copy["asset_id"], copy["asset_url"]))
                if len({item[0] for item in identified}) == 1:
                    (
                        source["publisher"],
                        source["publisher_locator"],
                        source["publisher_asset"],
                        source["publisher_url"],
                    ) = identified[0]
            evidence.append(source)
    fields, proof = {}, {}
    names = {s["publisher"] for s in evidence}
    if len(names) == 1 and "" not in names:
        fields["publisher"] = next(iter(names))
        proof["publisher"] = [
            {
                "asset_id": s["publisher_asset"],
                "document_id": s["document_id"],
                "fragment_id": s["fragment_id"],
                "locator": s["publisher_locator"],
                "quote": s["publisher"],
                "url": s["publisher_url"],
                "method": "archive",
            }
            for s in evidence
        ]
    for key in ("source", "format", "stage"):
        value = values.get(key)
        if not isinstance(value, str):
            continue
        citations = [r for r in field_refs if r["field"] in (key, "attributes." + key)]
        for s in evidence:
            excerpts = [r["quote"] for r in citations if r["fragment_id"] == s["fragment_id"]]
            if citations and not excerpts:
                continue
            for quote in excerpts or [s["quote"]]:
                if supported(key, value, s, quote):
                    fields[key] = value
                    proof[key] = [
                        {
                            "asset_id": s["asset_id"],
                            "document_id": s["document_id"],
                            "fragment_id": s["fragment_id"],
                            "locator": s["locator"],
                            "quote": quote,
                            "url": s["url"],
                            "method": "proposal",
                        }
                    ]
                    break
            if key in proof:
                break
    return fields, proof


def refresh(c, version):
    row = one(
        c,
        """SELECT d.attributes,k.citations FROM world_record_data d JOIN claims k ON k.id=d.version_id
                 WHERE d.version_id=?""",
        (version,),
    )
    if not row:
        return
    refs = rows(c, "SELECT field,fragment_id,quote FROM field_evidence WHERE version_id=?", (version,))
    fields, proof = verify(c, json.loads(row["citations"]), json.loads(row["attributes"]), refs)
    c.execute("INSERT OR REPLACE INTO card_provenance VALUES(?,?,?)", (version, dump(fields), dump(proof)))


def read(c, version, values):
    stored = one(c, "SELECT fields,evidence FROM card_provenance WHERE version_id=?", (version,))
    if not stored:
        return values, {}
    return {
        **{k: v for k, v in values.items() if k not in CARD_FIELDS},
        **json.loads(stored["fields"]),
    }, json.loads(stored["evidence"])
