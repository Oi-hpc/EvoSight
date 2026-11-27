"""Public filing indexes and dated exchange sessions with publisher-level evidence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlsplit

from . import feeds

SEC_PATH = re.compile(r"/submissions/CIK(\d{10})\.json")
HISTORY_PATH = re.compile(r"/stocks/([a-z0-9.-]+)/history/?", re.I)
YAHOO_PATH = re.compile(r"/quote/([a-z0-9.-]+)/history/?", re.I)
MONEY = re.compile(r"\d+(?:\.\d+)?")
MARKET = re.compile(r"(?:NASDAQ|NYSE(?:ARCA)?|NYSE AMERICAN)\s*:\s*([A-Z0-9.-]+)\s*[·|].{0,80}?\bUSD\b", re.I)


def source_kind(url):
    u = urlsplit(url)
    if u.hostname == "data.sec.gov" and SEC_PATH.fullmatch(u.path):
        return "sec_filings"
    if u.hostname == "stockanalysis.com" and HISTORY_PATH.fullmatch(u.path):
        return "daily_prices"
    if u.hostname == "finance.yahoo.com" and YAHOO_PATH.fullmatch(u.path):
        return "daily_prices"
    return None


def filings(resource):
    matched = SEC_PATH.fullmatch(urlsplit(resource.url).path)
    data = json.loads(resource.body)
    if not matched or str(data.get("cik", "")).zfill(10) != matched[1] or not data.get("name"):
        raise ValueError("SEC 申报索引的发行人身份无法核对")
    recent = data.get("filings", {}).get("recent", {})
    columns = ("form", "filingDate", "acceptanceDateTime", "accessionNumber", "primaryDocument")
    issuer = data["name"]
    symbol = ", ".join(data.get("tickers", []))
    entries = []
    for form, day, accepted, accession, document in zip(*(recent.get(k, []) for k in columns)):
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(
            r"[\w.-]+(?:/[\w.-]+)*", document
        ):
            continue
        if any(part in (".", "..") for part in document.split("/")):
            continue
        stamp = feeds.timestamp(accepted)
        if not stamp:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(matched[1])}/{accession.replace('-','')}/{document}"
        text = f"{issuer} ({symbol}) · SEC Form {form}. Filing date {day}. Accepted {accepted}. Accession {accession}. Primary document {document}."
        entries.append(
            {
                "url": url,
                "native_id": accession,
                "title": f"{issuer} · SEC {form} · {day} · {accession}",
                "summary": text,
                "published_at": stamp,
                "evidence_locator": f"SEC submissions · {accession}",
            }
        )
        if len(entries) >= 16:
            break
    if not entries:
        raise ValueError("SEC 未返回可核验的申报记录")
    return entries


def close_stamp(tree, latest_day):
    """Use a closing time only when the publisher explicitly gives the date, clock and zone."""
    found = tree.xpath(
        "//*[not(self::script or self::style) and starts-with(normalize-space(text()),'At close:')]/parent::*"
    )
    for element in found:
        value = " ".join(element.text_content().split())
        m = re.search(
            r"At close:\s*([A-Z][a-z]{2,8} \d{1,2}(?:, \d{4})?)(?:,| at)?\s+(\d{1,2}:\d{2}(?::\d{2})? [AP]M)\s+(EST|EDT)\b",
            value,
        )
        if not m:
            continue
        date_text, clock, zone = m.groups()
        if "," not in date_text and latest_day:
            date_text += f", {latest_day.year}"
        day = parse_day(date_text)
        if not day or day != latest_day:
            continue
        fmt = "%I:%M:%S %p" if clock.count(":") == 2 else "%I:%M %p"
        hour = datetime.strptime(clock, fmt).time()
        instant = datetime.combine(day, hour, timezone(timedelta(hours=-5 if zone == "EST" else -4)))
        return day, feeds.timestamp(instant.isoformat())
    return None, None


def parse_day(value):
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def daily_prices(resource, tree):
    url = urlsplit(resource.url)
    yahoo = url.hostname == "finance.yahoo.com"
    matched = (YAHOO_PATH if yahoo else HISTORY_PATH).fullmatch(url.path)
    if not matched:
        raise ValueError("行情表的证券标识无效")
    symbol = matched[1].upper()
    name = " ".join(tree.xpath("string(//h1)").split())
    if yahoo:
        title = re.fullmatch(r"(.+)\s+\(([A-Z0-9.-]+)\)", name)
        exchange_lines = tree.xpath(
            "//*[not(self::script or self::style) and (starts-with(normalize-space(text()),'Nasdaq') or starts-with(normalize-space(text()),'NYSE'))]/parent::*"
        )
        confirmed = any(
            re.search(
                r"(?:Nasdaq|NYSE)[\w ]*\s*-\s*[^•]{1,70}•\s*USD\b", " ".join(el.text_content().split()), re.I
            )
            for el in exchange_lines
        )
        issuer = title[1] if title and title[2].upper() == symbol and confirmed else ""
    else:
        issuer = re.sub(r"\s+Stock Price History.*$", "", name, flags=re.I).strip()
        exchange_lines = tree.xpath(
            "//*[not(self::script or self::style) and (contains(text(),'NASDAQ:') or contains(text(),'NYSE:') or contains(text(),'NYSEARCA:') or contains(text(),'NYSE American:'))]/text()"
        )
        identifiers = [
            m[1].upper() for line in exchange_lines if (m := MARKET.search(" ".join(line.split())))
        ]
        if symbol not in identifiers:
            issuer = ""
    if not issuer:
        raise ValueError("行情页未同时确认发行人、交易所代码和美元计价")
    required = ("date", "open", "high", "low", "close", "volume")
    for table in tree.xpath("//table"):
        headers = [
            " ".join(x.text_content().split()).casefold()
            for x in table.xpath("(.//tr)[1]/*[self::th or self::td]")
        ]
        headers = ["close" if h.startswith("close close price ") else h for h in headers]
        if not set(required) <= set(headers):
            continue
        entries = []
        first = table.xpath("(.//tr[td])[1]/td[1]")
        close_day, as_of = close_stamp(tree, parse_day(first[0].text_content().strip()) if first else None)
        for row in table.xpath(".//tr[td]"):
            cells = [" ".join(x.text_content().split()) for x in row.xpath("./td")]
            record = dict(zip(headers, cells))
            day = parse_day(record.get("date", ""))
            if not day or not all(
                MONEY.fullmatch(record.get(k, "").replace(",", "")) for k in ("open", "high", "low", "close")
            ):
                continue
            try:
                o, h, l, close = [
                    Decimal(record[k].replace(",", "")) for k in ("open", "high", "low", "close")
                ]
                volume = int(record["volume"].replace(",", ""))
            except (InvalidOperation, ValueError, KeyError):
                continue
            if l <= 0 or not l <= min(o, close) <= max(o, close) <= h or volume < 0:
                continue
            daystr = day.isoformat()
            detail = f"{issuer} ({symbol}) · USD · trading session {daystr}. Open {o}, high {h}, low {l}, close {close}, volume {volume}."
            when = as_of if day == close_day else None
            entries.append(
                {
                    "url": resource.url,
                    "native_id": f"{symbol}/{daystr}",
                    "distinct_native": True,
                    "title": f"{issuer} ({symbol}) · {daystr} 收盘 {close} USD",
                    "summary": detail,
                    "published_at": None,
                    "source_updated_at": when,
                    "published_label": daystr,
                    "evidence_locator": f"Stock price history · {symbol} · {daystr}",
                }
            )
            if len(entries) >= 15:
                break
        if entries:
            return entries
    raise ValueError("未找到包含交易日、开高低收和成交量的有效日线表")
