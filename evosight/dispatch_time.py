"""Resolve a one-off dispatch from the user's own time expression and chat timezone."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import utc

DIGITS = r"[0-9零一二两三四五六七八九十百]+"


def number(text):
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    text = text.replace("两", "二")
    for symbol, multiplier in (("百", 100), ("十", 10)):
        if symbol in text:
            first, rest = text.split(symbol, 1)
            return (number(first) or 1) * multiplier + number(rest.lstrip("零"))
    if len(text) == 1 and text in "零一二三四五六七八九":
        return "零一二三四五六七八九".index(text)
    raise ValueError("请给出明确的日期和时间。")


def resolve(expression, ctx):
    zone_name = ctx.get("timezone")
    try:
        zone = (
            ZoneInfo(zone_name)
            if zone_name
            else timezone(timedelta(minutes=ctx.get("utc_offset_minutes", 0)))
        )
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone(timedelta(minutes=ctx.get("utc_offset_minutes", 0)))
    now = datetime.fromisoformat(ctx["asked_at"].replace("Z", "+00:00")).astimezone(zone)
    raw = expression.strip()
    future = re.fullmatch(r"(" + DIGITS + r")\s*(分(?:钟)?|小时|天)(?:之)?后", raw)
    if future:
        delta = number(future[1]) * {"分": 60, "分钟": 60, "小时": 3600, "天": 86400}[future[2]]
        dt = now + timedelta(seconds=delta)
    else:
        try:
            if not re.search(r"\d[T ]\d{1,2}:\d{2}", raw):
                raise ValueError()
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=zone)
        except ValueError:
            day = re.fullmatch(
                r"(今天?|明天|后天|(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号])\s*"
                r"(早上|上午|中午|下午|晚上|凌晨)?\s*(" + DIGITS + r")(?:[点时:：])(?:\s*(半|一刻|三刻|" + DIGITS + r")(?:分)?)?",
                raw,
            )
            if not day:
                raise ValueError("请给出具体执行时间，例如「明天下午三点」「两小时后」或带时区的完整时间。")
            if day[3]:
                target = now.date().replace(year=int(day[2] or now.year), month=int(day[3]), day=int(day[4]))
            else:
                target = now.date() + timedelta(days={"今": 0, "今天": 0, "明天": 1, "后天": 2}[day[1]])
            h = number(day[6])
            if day[5] in ("下午", "晚上") and h < 12:
                h += 12
            if day[5] == "凌晨" and h == 12:
                h = 0
            if day[5] == "中午" and h in (1, 2):
                h += 12
            m = {"半": 30, "一刻": 15, "三刻": 45}.get(day[7])
            if m is None:
                m = number(day[7])
            dt = datetime.combine(target, time(h, m), tzinfo=zone)
    if dt <= now:
        raise ValueError("该时间已经过去，请指定未来的执行时间。")
    return utc(dt)
