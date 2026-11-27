"""Read attributable, public posts and the public pages that establish account ownership."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import os
import re
from urllib.parse import quote, urlencode, urlsplit

from . import feeds, materials


SITES = {"x": "x.com", "weibo": "weibo.com", "xiaohongshu": "xiaohongshu.com"}
LABELS = {"x": "X", "weibo": "微博", "xiaohongshu": "小红书", "news": "新闻与公告", "history": "历史资料"}
HOSTS = {
    "x.com": "x",
    "twitter.com": "x",
    "www.x.com": "x",
    "www.twitter.com": "x",
    "mobile.twitter.com": "x",
    "weibo.com": "weibo",
    "www.weibo.com": "weibo",
    "m.weibo.cn": "weibo",
    "weibo.cn": "weibo",
    "xiaohongshu.com": "xiaohongshu",
    "www.xiaohongshu.com": "xiaohongshu",
}
RESERVED_X = {"i", "intent", "search", "home", "explore", "settings", "messages", "share"}
RESERVED_WEIBO = {"detail", "status", "search", "login", "signup", "newlogin", "tv", "ajax", "u", "n"}


def location(url, *, post=False):
    """Normalize only recognizable public account or post URLs; remove tracking tokens."""
    safe = materials.safe_link("", url)
    u = urlsplit(safe)
    platform = HOSTS.get(u.hostname)
    path = u.path.strip("/")
    if platform == "x":
        path = path.removeprefix("@")
        m = re.fullmatch(
            r"([A-Za-z0-9_]{1,15})/status/(\d+)(?:/(?:photo|video)/\d+)?"
            if post
            else r"([A-Za-z0-9_]{1,15})",
            path,
        )
        if m and m[1].lower() not in RESERVED_X:
            key = m[1].lower()
            return platform, key, "https://x.com/" + key + ("/status/" + m[2] if post else "")
    elif platform == "weibo":
        if post:
            m = re.fullmatch(r"(\d+)/([A-Za-z0-9]+)", path)
            if m:
                return platform, m[1], f"https://weibo.com/{m[1]}/{m[2]}"
            m = re.fullmatch(r"(?:detail|status)/([A-Za-z0-9]+)", path)
            if m:
                return platform, "", "https://" + u.hostname + "/" + path
        else:
            m = re.fullmatch(r"(?:u/)?(\d+)", path)
            if m:
                return platform, m[1], "https://weibo.com/u/" + m[1]
            m = re.fullmatch(r"(?:n/)?([A-Za-z][A-Za-z0-9_]{1,40})", path)
            if m and m[1].lower() not in RESERVED_WEIBO:
                return platform, m[1].lower(), "https://weibo.com/" + m[1]
    elif platform == "xiaohongshu":
        m = re.fullmatch(
            r"(?:explore|discovery/item)/([A-Za-z0-9]{12,40})"
            if post
            else r"user/profile/([A-Za-z0-9]{12,40})",
            path,
        )
        if m:
            return (
                platform,
                "" if post else m[1],
                "https://www.xiaohongshu.com/" + ("explore/" if post else "user/profile/") + m[1],
            )
    return None


def absolute_time(value):
    return feeds.timestamp(value) if isinstance(value, str) else None


def objects(root):
    for raw in root.xpath('//script[@type="application/ld+json"]/text()')[:12]:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            continue
        queue = items if isinstance(items, list) else [items]
        for obj in queue[:25]:
            if isinstance(obj, dict):
                yield obj
                for sub in obj.get("@graph", []) or []:
                    if isinstance(sub, dict):
                        yield sub


@dataclass
class Post:
    resource: feeds.Resource
    extract: materials.Extract
    author: str
    repost: bool = False

    def item(self):
        return {
            "title": self.extract.title,
            "url": self.resource.url,
            "published_at": self.extract.published_at,
            "summary": " ".join(p[1] for p in self.extract.parts)[:3200],
        }


def post_record(url, text, title, author, at, body, mime, repost=False):
    text = materials.clean_text(text, 3500)
    if len(text) < 5:
        raise ValueError("未取得可定位的公开帖正文")
    ex = materials.Extract("public_post", title=materials.clean_text(title or text, 200), published_at=at)
    ex.parts = [("公开帖正文", text, url)]
    if at:
        ex.parts.append(("发布方时间", at, url))
    return Post(feeds.Resource(body, url, mime), ex, author, repost)


def read_post(acq, url, channel):
    loc = location(url, post=True)
    if not loc or loc[0] != channel:
        raise ValueError("没有对应平台的公开帖链接")
    canonical = loc[2]
    if channel == "x":
        res = acq.download(
            "https://publish.twitter.com/oembed?"
            + urlencode({"url": canonical, "omit_script": "1", "dnt": "true"})
        )
        raw = json.loads(res.body)
        author = location(raw.get("author_url", ""))
        if not author or author[:2] != loc[:2] or location(raw.get("url", ""), post=True) != loc:
            raise ValueError("平台返回的作者或帖子编号不一致")
        root = materials.tree_for(feeds.Resource(raw["html"].encode(), canonical, "text/html"))
        pars = root.xpath("//blockquote/p")
        if not pars:
            raise ValueError("X 公开嵌入未提供帖子正文")
        text = " ".join(p.text_content() for p in pars)
        times = root.xpath("//time/@datetime")
        return post_record(
            canonical,
            text,
            raw.get("author_name", "") + " · " + text[:100],
            author[1],
            absolute_time(times[0]) if times else None,
            res.body,
            res.mime,
            repost=bool(re.match(r"^(RT\s+@|转发微博)", text)),
        )
    res = acq.download(canonical)
    if location(res.url, post=True) != loc:
        raise ValueError("平台返回了登录或跳转页")
    root = materials.tree_for(res)

    def meta(key):
        return next(iter(root.xpath("//meta[@name=$key or @property=$key]/@content", key=key)), "")

    canonical_tag = meta("og:url") or next(iter(root.xpath('//link[@rel="canonical"]/@href')), "")
    if canonical_tag and location(canonical_tag, post=True) != loc:
        raise ValueError("页面声明的帖子地址与请求不符")
    post = next(
        (
            o
            for o in objects(root)
            if o.get("@type") in ("SocialMediaPosting", "DiscussionForumPosting", "BlogPosting", "Article")
        ),
        {},
    )
    author = post.get("author") or {}
    author = author[0] if isinstance(author, list) and author else author
    raw_author = author.get("url", "") if isinstance(author, dict) else ""
    raw_author = (
        raw_author or meta("article:author") or next(iter(root.xpath('//a[@rel="author"]/@href')), "")
    )
    owner = location(materials.safe_link(res.url, raw_author)) if raw_author else None
    if not owner:
        owners = {
            candidate_profile
            for href in root.xpath(
                '//*[contains(concat(" ",normalize-space(@class)," ")," author ") or contains(@class,"author-container") or contains(@class,"author-info")]//a/@href'
            )
            if (candidate_profile := location(materials.safe_link(res.url, href)))
            and candidate_profile[0] == channel
        }
        if len(owners) == 1:
            owner = owners.pop()
    if not owner and channel == "weibo" and loc[1] and (post or canonical_tag):
        owner = ("weibo", loc[1], "https://weibo.com/u/" + loc[1])
    date = absolute_time(
        post.get("datePublished")
        or meta("article:published_time")
        or next(
            iter(root.xpath('//article//time/@datetime | //time[@itemprop="datePublished"]/@datetime')), ""
        )
    )
    text = post.get("articleBody") or post.get("text") or meta("og:description")
    if not text:
        text = " ".join(root.xpath("//article//p//text()"))
    if not text or (not post and not canonical_tag and not owner):
        raise ValueError("平台未提供可核查的公开帖元数据和正文")
    if re.search(r"登录后(查看|浏览)|请先登录|登录即代表|访问频次过高|访问受限|安全验证|扫码登录|系统繁忙|Sign in to", text, re.I):
        raise ValueError("平台返回登录或受限提示")
    title = post.get("headline") or meta("og:title") or text[:80]
    return post_record(
        canonical,
        text,
        title,
        owner[1] if owner and owner[0] == channel else "",
        date,
        res.body,
        res.mime,
        repost=bool(post.get("sharedContent") or re.match(r"^(转发微博|//@|转载自)", text)),
    )


def profile_links(resource, names):
    """Keep a link's actual identity assertion and its publisher as public verification evidence."""
    if not resource.mime.lower().startswith(("text/html", "application/xhtml")):
        return []
    if HOSTS.get(urlsplit(resource.url).hostname):
        return []
    root = materials.tree_for(resource)
    norm = lambda s: " ".join(str(s).casefold().split())
    names = {norm(n) for n in names if n}
    found = {}

    def add(url, proof, strong):
        loc = location(materials.safe_link(resource.url, url))
        if loc and (loc not in found or strong):
            found[loc] = (loc, proof[:450], strong)

    for p in objects(root):
        if p.get("@type") == "Person" and norm(p.get("name", "")) in names:
            links = p.get("sameAs") or []
            for u in ([links] if isinstance(links, str) else links)[:12]:
                if isinstance(u, str):
                    add(u, f'{p["name"]} · Person.sameAs · {u}', True)
    heading = " ".join(root.xpath("//h1//text()")) or " ".join(root.xpath("//title/text()"))
    if any(n in norm(heading) for n in names):
        for a in root.xpath('//a[@href and contains(concat(" ",normalize-space(@rel)," ")," me ")]')[:15]:
            add(a.get("href"), heading + " · rel=me · " + a.get("href"), True)
        if norm(heading) in names and len(root.text_content()) < 16000:
            for a in root.xpath("//main//a[@href] | //footer//a[@href] | //nav//a[@href] | //p/a[@href]")[
                :75
            ]:
                parent = materials.clean_text(a.getparent().text_content(), 650)
                explicit = bool(
                    re.search(r"follow me on|my (?:public )?accounts?|我的(?:微博|小红书|账号)|关注我的", parent, re.I)
                )
                add(a.get("href"), heading + " · " + parent, explicit)
    for title in root.xpath("//h2|//h3|//h4|//*[@itemprop='name']")[:100]:
        if norm(title.text_content()) not in names:
            continue
        box = title.getparent()
        for _ in range(3):
            if box is None or len(box.xpath(".//h2|.//h3|.//h4")) > 1 or len(box.text_content()) > 1600:
                break
            for a in box.xpath(".//a[@href]")[:18]:
                add(a.get("href"), title.text_content() + " · 资料卡链接 " + a.get("href"), False)
            box = box.getparent()
    return list(found.values())


def x_json(path, token):
    # A fixed TLS origin and no redirects keep credentials out of article requests and archives.
    conn = http.client.HTTPSConnection("api.x.com", timeout=8)
    try:
        conn.request("GET", path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise ValueError(f"X 用户公开时间线 HTTP {resp.status}")
        data = resp.read(512 * 1024 + 1)
        if len(data) > 512 * 1024:
            raise ValueError("X 时间线响应超过本轮上限")
        return json.loads(data)
    finally:
        conn.close()


def x_timeline(account, start, end):
    token = os.environ.get("EVOSIGHT_X_BEARER_TOKEN")
    if not token:
        return None
    name = account["account_key"]
    user = x_json(
        "/2/users/by/username/" + quote(name, safe="") + "?user.fields=protected,username", token
    ).get("data", {})
    uid = str(user.get("id", ""))
    if user.get("protected") is not False or not uid.isdigit() or user.get("username", "").casefold() != name:
        raise ValueError("未能核实 X 公开账号")
    params = {
        "start_time": start,
        "end_time": end,
        "max_results": "20",
        "tweet.fields": "created_at,author_id,referenced_tweets",
        "exclude": "retweets",
    }
    result = x_json("/2/users/" + uid + "/tweets?" + urlencode(params), token)
    if result.get("errors"):
        raise ValueError("X 公开时间线返回不完整结果")
    posts = []
    for obj in result.get("data", []):
        if str(obj.get("author_id")) != uid or not str(obj.get("id", "")).isdigit():
            continue
        url = f'https://x.com/{name}/status/{obj["id"]}'
        posts.append(
            post_record(
                url,
                obj["text"],
                "@" + name + " · " + obj["text"][:100],
                name,
                absolute_time(obj.get("created_at")),
                json.dumps(obj, ensure_ascii=False).encode(),
                "application/json",
                any(s.get("type") == "retweeted" for s in obj.get("referenced_tweets", [])),
            )
        )
    return posts, bool(result.get("meta", {}).get("next_token"))
