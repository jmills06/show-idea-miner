#!/usr/bin/env python3
"""
Show Idea Miner - Phase 1 (collection + trend tracking, no AI layer yet)

Sources: Reddit (official OAuth API), Hacker News (Algolia), Mastodon hashtags,
Amateur Radio Stack Exchange, groups.io mailing lists, Discourse forums, and
forum/blog RSS. YouTube supplies competitive research and the FCC equipment
authorization database supplies gear scoops; both ride alongside the items
rather than in the display rotation.
Filters noise, dedupes against previous runs, tracks term trends over time.

Writes:
  - ideas.json            (latest batch + current trends; the display reads this)
  - ideas/YYYY-MM-DD.json (dated archive copy)
  - seen.json             (post IDs already collected, for cross-run dedup)
  - term_history.json     (rolling daily term counts, fuel for trend detection)

Every source degrades gracefully: a missing credential, a dead host, or a
renamed feed logs a warning and the run continues without it. Optional repo
secrets: REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET, YOUTUBE_API_KEY,
STACKEXCHANGE_KEY. Pure standard library, no installs.

Run a single collector without writing anything:
    python scripts/mine_ideas.py --only=stackexchange
"""

import base64
import gzip
import hashlib
import json
import os
import re
import sys
import time
import html as html_lib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------
# Working constants (tune here, not deep in the code)
# ----------------------------------------------------------------------

USER_AGENT = "github-actions:everyday-ham-idea-miner:v1.1 (by /u/jmills06)"

SUBREDDITS = ["amateurradio", "hamradio", "morse", "RTLSDR"]
REDDIT_SORT = "top"
REDDIT_TIMEFRAME = "week"
REDDIT_LIMIT = 25
REDDIT_MIN_SCORE = 25        # ignore posts below this many upvotes
REDDIT_MIN_COMMENTS = 10     # OR below this many comments (must pass one)

HN_QUERIES = [
    "ham radio",
    "amateur radio",
    "software defined radio",
    "shortwave",
    "Meshtastic",
    "LoRa radio",
    "RTL-SDR",
    "HackRF",
    "GNU Radio",
    "WSPR",
    "Morse code",
    "Baofeng",
    "APRS",
    "FT8",
]
HN_MIN_POINTS = 10
HN_DAYS_BACK = 14

MASTODON_INSTANCE = "https://mastodon.social"
MASTODON_TAGS = [
    "amateurradio", "hamradio",
    "sota", "qrp", "morsecode",
    "fieldday", "hamfest", "amsat",
]
MASTODON_LIMIT = 40          # per tag (API max)
MASTODON_MIN_ENGAGEMENT = 3  # boosts + favorites + replies must reach this

# --- YouTube competitive research (needs YOUTUBE_API_KEY secret) ---
YOUTUBE_QUERIES = [
    "ham radio", "amateur radio", "POTA parks on the air",
    "Icom", "Yaesu", "Xiegu", "Elecraft", "FlexRadio",
    "Meshtastic", "Hamvention", "Friedrichshafen ham radio",
]
YOUTUBE_DAYS_BACK = 14       # recent uploads only
YOUTUBE_PER_QUERY = 10
YOUTUBE_MIN_VIEWS = 2000     # ignore videos below this
EXCLUDE_CHANNELS = ["everyday ham"]  # don't report our own videos back to us

# --- Forum / blog RSS feeds ---
# The Action log reports per-feed results; prune or extend freely.
RSS_FEEDS = [
    ("QRZ Forums", "https://forums.qrz.com/index.php?forums/-/index.rss"),
    ("Hackaday", "https://hackaday.com/tag/ham-radio/feed/"),
]
RSS_DAYS_BACK = 7            # ignore entries older than this

# --- Amateur Radio Stack Exchange (questions are pain points are show topics) ---
# No key needed under 300 requests/day per IP. Set the STACKEXCHANGE_KEY repo
# secret to raise that to 10,000/day if the shared Actions IPs run dry.
STACKEXCHANGE_SITES = ["ham"]
STACKEXCHANGE_DAYS_BACK = 14
STACKEXCHANGE_PAGESIZE = 50
STACKEXCHANGE_MIN_VIEWS = 30   # skip questions nobody has even looked at

# --- groups.io mailing lists ---
# Slugs are the part after groups.io/g/. Public groups only: a private or
# misspelled group logs a failure and is skipped, so prune from the Action log.
GROUPS_IO_FEED = "https://groups.io/g/{group}/rss"
GROUPS_IO_GROUPS = [
    "Elecraft",
    "QRPLabs",
    "Xiegu",
    "sBitx",
    "BITX20",
    "TAPR",
]
GROUPS_IO_DAYS_BACK = 7
GROUPS_IO_MIN_MESSAGES = 1     # 1 keeps every thread; raise to demand replies

# --- Discourse forums (any Discourse site exposes /latest.json, no key) ---
DISCOURSE_SITES = [
    ("GNU Radio", "https://discourse.gnuradio.org"),
    ("Meshtastic", "https://meshtastic.discourse.group"),
    ("Digirig", "https://forum.digirig.net"),
]
DISCOURSE_DAYS_BACK = 14
DISCOURSE_MIN_REPLIES = 2      # a topic must clear replies OR views, not both
DISCOURSE_MIN_VIEWS = 100

# --- FCC equipment authorizations (scoops, not discussion) ---
# New radios are type-certified here before the manufacturer announces them.
# The OET search only returns HTML, so collect_fcc parses defensively; see the
# comment above it before tuning.
FCC_ENABLED = True
FCC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearchResult.cfm"
FCC_APPLICANTS = [
    "Icom", "Yaesu", "Kenwood", "Alinco", "Xiegu",
    "Elecraft", "FlexRadio", "Anytone", "Retevis", "Radioddity",
]
FCC_DAYS_BACK = 45
FCC_MAX_PER_APPLICANT = 6

MAX_TOTAL_ITEMS = 80
MAX_PER_SOURCE = 18          # keeps one chatty feed from filling the batch
SEEN_RETENTION_DAYS = 45
REQUEST_TIMEOUT = 20
RETRIES = 3
RETRY_WAIT = 5

# --- Trend tracking ---
TREND_RECENT_DAYS = 7        # "now" window
TREND_BASELINE_DAYS = 21     # comparison window before that
TREND_MIN_MENTIONS = 3       # term must appear in this many recent posts
TREND_HISTORY_DAYS = 60      # prune term history older than this
TREND_TOP_N = 8              # how many trends to publish

# Words too common in this niche to ever be a "trend"
STOPWORDS = set("""
a an the and or but if then than so of for to in on at by with from as is are
was were be been being have has had do does did will would can could should my
your his her its our their this that these those it he she they we you i me
about into over under after before out up down off just only also very really
what when where which who whom whose why how not no yes new old get got make
made using use used vs via any all some more most other another first last
ham radio amateur
question questions help advice tips recommendations recommendation anyone
best good great looking need wanted want trying thoughts opinions
building build built setup getting started guide review thread discussion
practice methods method finally today week time day going
hamradio amateurradio hamradioclub
sota qrp morsecode fieldday hamfest amsat
one two three like there here com http https www html org net
band bands radio radios get going make made way back good best
new old big small first last next still even much many lot
day days week weeks year years today tonight now then
post posts thread threads comment comments reply replies
like just dont don cant can will would could should into over
amp watt watts via per etc inc com
""".split())

# Repo root is one level up from scripts/
ROOT = Path(__file__).resolve().parent.parent
IDEAS_FILE = ROOT / "ideas.json"
ARCHIVE_DIR = ROOT / "ideas"
SEEN_FILE = ROOT / "seen.json"
TERM_HISTORY_FILE = ROOT / "term_history.json"

# ----------------------------------------------------------------------
# HTTP helper with retries
# ----------------------------------------------------------------------

def fetch_bytes(url, headers=None, data=None, label=None):
    """Fetch raw bytes with retries, transparently gunzipping the reply.
    Stack Exchange always gzips; urllib does not decompress on its own."""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return raw
        except Exception as e:
            print(f"  [warn] attempt {attempt}/{RETRIES} failed for "
                  f"{label or url}: {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT)
    return None


def fetch_json(url, headers=None, data=None):
    raw = fetch_bytes(url, headers=headers, data=data)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"  [warn] unparseable JSON from {url}: {e}")
        return None

# ----------------------------------------------------------------------
# Collectors
# ----------------------------------------------------------------------

def reddit_get_token():
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        print("[reddit] credentials not set; skipping Reddit this run")
        return None
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    result = fetch_json(
        "https://www.reddit.com/api/v1/access_token",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    if result and "access_token" in result:
        print("[reddit] OAuth token acquired")
        return result["access_token"]
    print(f"[reddit] ERROR: token request failed: {result}")
    return None


def collect_reddit():
    items = []
    token = reddit_get_token()
    if not token:
        return items
    headers = {"Authorization": f"Bearer {token}"}
    for sub in SUBREDDITS:
        url = (f"https://oauth.reddit.com/r/{sub}/{REDDIT_SORT}"
               f"?t={REDDIT_TIMEFRAME}&limit={REDDIT_LIMIT}")
        print(f"[reddit] r/{sub} ...")
        data = fetch_json(url, headers=headers)
        if not data:
            print(f"  [warn] skipping r/{sub}, no data")
            continue
        kept = 0
        for child in data.get("data", {}).get("children", []):
            p = child.get("data", {})
            score = p.get("score", 0)
            comments = p.get("num_comments", 0)
            if score < REDDIT_MIN_SCORE and comments < REDDIT_MIN_COMMENTS:
                continue
            if p.get("stickied") or p.get("over_18"):
                continue
            items.append({
                "id": f"reddit_{p.get('id')}",
                "source": f"r/{sub}",
                "title": (p.get("title") or "").strip(),
                "url": f"https://www.reddit.com{p.get('permalink', '')}",
                "score": score,
                "comments": comments,
                "created_utc": int(p.get("created_utc", 0)),
                "blurb": truncate_words((p.get("selftext") or "").strip(), 220),
            })
            kept += 1
        print(f"  kept {kept}")
        time.sleep(2)
    print(f"[reddit] total kept: {len(items)}")
    return items


def collect_hackernews():
    items = []
    cutoff = int(time.time()) - HN_DAYS_BACK * 86400
    for q in HN_QUERIES:
        params = urllib.parse.urlencode({
            "query": f'"{q}"',
            "tags": "story",
            "numericFilters": f"points>{HN_MIN_POINTS},created_at_i>{cutoff}",
            "hitsPerPage": 20,
        })
        data = fetch_json(f"https://hn.algolia.com/api/v1/search?{params}")
        if not data:
            print(f"[hn] query: {q} ... request failed")
            continue
        hits = data.get("hits", [])
        print(f"[hn] query: {q} ... {data.get('nbHits', 0)} matches, {len(hits)} returned")
        for hit in hits:
            items.append({
                "id": f"hn_{hit.get('objectID')}",
                "source": "Hacker News",
                "title": (hit.get("title") or "").strip(),
                "url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "score": hit.get("points", 0) or 0,
                "comments": hit.get("num_comments", 0) or 0,
                "created_utc": hit.get("created_at_i", 0),
                "blurb": "",
            })
        time.sleep(1)
    print(f"[hn] total kept: {len(items)} (pre-dedup)")
    return items


def strip_html(text):
    """Mastodon post content arrives as HTML; flatten it to plain text."""
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"</p>\s*<p>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def collect_mastodon():
    items = []
    for tag in MASTODON_TAGS:
        url = f"{MASTODON_INSTANCE}/api/v1/timelines/tag/{tag}?limit={MASTODON_LIMIT}"
        data = fetch_json(url)
        if not isinstance(data, list):
            print(f"[mastodon] #{tag} ... request failed")
            continue
        kept = 0
        for status in data:
            engagement = ((status.get("reblogs_count") or 0)
                          + (status.get("favourites_count") or 0)
                          + (status.get("replies_count") or 0))
            if engagement < MASTODON_MIN_ENGAGEMENT:
                continue
            if status.get("sensitive"):
                continue
            text = strip_html(status.get("content") or "")
            if not text:
                continue
            items.append({
                "id": f"mastodon_{status.get('id')}",
                "source": f"Mastodon #{tag}",
                "title": truncate_words(text, 120),
                "url": status.get("url") or "",
                "score": engagement,
                "comments": status.get("replies_count") or 0,
                "created_utc": parse_iso(status.get("created_at")),
                "blurb": truncate_words(text, 220),
            })
            kept += 1
        print(f"[mastodon] #{tag} ... {len(data)} fetched, kept {kept}")
        time.sleep(1)
    print(f"[mastodon] total kept: {len(items)} (pre-dedup)")
    return items

# ----------------------------------------------------------------------
# YouTube (competitive research; separate from the display rotation)
# ----------------------------------------------------------------------

def collect_youtube():
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        print("[youtube] YOUTUBE_API_KEY not set; skipping YouTube this run")
        return []
    published_after = datetime.fromtimestamp(
        time.time() - YOUTUBE_DAYS_BACK * 86400, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    video_ids, meta = [], {}
    for q in YOUTUBE_QUERIES:
        params = urllib.parse.urlencode({
            "part": "snippet", "q": q, "type": "video",
            "order": "viewCount", "publishedAfter": published_after,
            "maxResults": YOUTUBE_PER_QUERY, "key": api_key,
        })
        data = fetch_json(f"https://www.googleapis.com/youtube/v3/search?{params}")
        if not data:
            print(f"[youtube] query: {q} ... request failed")
            continue
        hits = data.get("items", [])
        print(f"[youtube] query: {q} ... {len(hits)} videos")
        for v in hits:
            vid = (v.get("id") or {}).get("videoId")
            sn = v.get("snippet") or {}
            if not vid or vid in meta:
                continue
            channel = (sn.get("channelTitle") or "").strip()
            if any(x in channel.lower() for x in EXCLUDE_CHANNELS):
                continue
            meta[vid] = {
                "title": html_lib.unescape((sn.get("title") or "").strip()),
                "channel": channel,
                "published": sn.get("publishedAt", ""),
            }
            video_ids.append(vid)
        time.sleep(1)
    if not video_ids:
        return []
    # stats calls in chunks of 50 ids (cheap: 1 quota unit per chunk)
    stat_items = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        params = urllib.parse.urlencode({
            "part": "statistics", "id": ",".join(chunk), "key": api_key})
        stats = fetch_json(f"https://www.googleapis.com/youtube/v3/videos?{params}")
        stat_items.extend((stats or {}).get("items", []))
    results = []
    for v in stat_items:
        vid = v.get("id")
        st = v.get("statistics") or {}
        views = int(st.get("viewCount", 0) or 0)
        if views < YOUTUBE_MIN_VIEWS or vid not in meta:
            continue
        results.append({
            "id": f"youtube_{vid}",
            "title": meta[vid]["title"],
            "channel": meta[vid]["channel"],
            "url": f"https://www.youtube.com/watch?v={vid}",
            "views": views,
            "comments": int(st.get("commentCount", 0) or 0),
            "published": meta[vid]["published"],
        })
    results.sort(key=lambda r: r["views"], reverse=True)
    print(f"[youtube] kept {len(results)} videos over {YOUTUBE_MIN_VIEWS} views")
    return results

# ----------------------------------------------------------------------
# Feed parsing (shared by the blog RSS feeds and the groups.io lists)
# ----------------------------------------------------------------------

WP_BOILERPLATE = re.compile(r"The post .{0,200}? appeared first on .{0,100}?\.",
                            re.IGNORECASE | re.DOTALL)


def _local(tag):
    """ElementTree keeps the namespace on the tag; we want the local name."""
    return str(tag).rsplit("}", 1)[-1]


def _entry_text(node, *names):
    for child in node:
        if _local(child.tag) in names:
            text = (child.text or "").strip()
            if text:
                return text
    return ""


def _entry_link(node):
    for child in node:
        if _local(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()      # Atom
        if href:
            return href
        if (child.text or "").strip():                # RSS 2.0
            return child.text.strip()
    return ""


def parse_feed_date(value):
    """Feeds date entries RFC 2822 (RSS) or ISO 8601 (Atom)."""
    if value:
        try:
            return parsedate_to_datetime(value).timestamp()
        except Exception:
            pass
        iso = parse_iso(value)
        if iso:
            return float(iso)
    return time.time()          # undated entries assumed fresh


def parse_feed(raw):
    """Yield (title, link, description, timestamp) from RSS or Atom bytes."""
    root = ET.fromstring(raw)
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = html_lib.unescape(_entry_text(node, "title")).strip()
        link = _entry_link(node)
        desc = strip_html(_entry_text(node, "description", "summary",
                                      "content", "encoded"))
        ts = parse_feed_date(_entry_text(node, "pubDate", "published",
                                         "updated", "date"))
        yield title, link, desc.strip(), ts


def collect_rss():
    items = []
    cutoff = time.time() - RSS_DAYS_BACK * 86400
    for name, url in RSS_FEEDS:
        raw = fetch_bytes(url, label=f"{name} feed")
        if raw is None:
            print(f"[rss] {name} ... failed")
            continue
        try:
            entries = list(parse_feed(raw))
        except Exception as e:
            print(f"[rss] {name} ... unparseable: {e}")
            continue
        kept = 0
        for title, link, desc, ts in entries:
            desc = WP_BOILERPLATE.sub("", desc).strip()
            if ts < cutoff or not title or not link:
                continue
            items.append({
                "id": "rss_" + hashlib.md5(link.encode()).hexdigest()[:12],
                "source": name,
                "title": title,
                "url": link,
                "score": 0,
                "comments": 0,
                "created_utc": int(ts),
                "blurb": truncate_words(desc, 220),
            })
            kept += 1
        print(f"[rss] {name} ... kept {kept}")
        time.sleep(1)
    print(f"[rss] total kept: {len(items)} (pre-dedup)")
    return items

# ----------------------------------------------------------------------
# groups.io mailing lists
# ----------------------------------------------------------------------

LIST_TAG_RE = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")
REPLY_PREFIX_RE = re.compile(r"^\s*(?:re|fwd|fw|aw)\s*:\s*", re.IGNORECASE)


def thread_subject(title):
    """Collapse '[Elecraft] Re: KX3 noise' and 'Re: KX3 noise' onto one thread."""
    previous = None
    while previous != title:
        previous = title
        title = LIST_TAG_RE.sub("", title)
        title = REPLY_PREFIX_RE.sub("", title)
    return " ".join(title.split())


def collect_groups_io():
    """Mailing list traffic, collapsed so one thread is one item and the
    reply count becomes the engagement signal."""
    items = []
    cutoff = time.time() - GROUPS_IO_DAYS_BACK * 86400
    for group in GROUPS_IO_GROUPS:
        raw = fetch_bytes(GROUPS_IO_FEED.format(group=group),
                          label=f"groups.io/{group}")
        if raw is None:
            print(f"[groups.io] {group} ... failed (private group or bad slug?)")
            continue
        try:
            entries = list(parse_feed(raw))
        except Exception as e:
            print(f"[groups.io] {group} ... unparseable: {e}")
            continue
        threads = {}
        for title, link, desc, ts in entries:
            if ts < cutoff or not title or not link:
                continue
            subject = thread_subject(title)
            if not subject:
                continue
            key = subject.lower()
            thread = threads.get(key)
            if thread is None:
                threads[key] = {"subject": subject, "link": link,
                                "blurb": desc, "ts": ts, "messages": 1}
                continue
            thread["messages"] += 1
            if ts < thread["ts"]:              # keep the thread opener
                thread["ts"] = ts
                thread["link"] = link
                if desc:
                    thread["blurb"] = desc
        kept = 0
        for key, thread in threads.items():
            if thread["messages"] < GROUPS_IO_MIN_MESSAGES:
                continue
            # The id carries a coarse activity bucket so a thread resurfaces
            # once it picks up real traction, instead of being permanently
            # suppressed by seen.json after its very first message.
            bucket = min(thread["messages"] // 5, 20)
            digest = hashlib.md5(f"{group}:{key}".encode()).hexdigest()[:10]
            items.append({
                "id": f"groupsio_{digest}_{bucket}",
                "source": f"groups.io · {group}",
                "title": thread["subject"],
                "url": thread["link"],
                "score": thread["messages"],
                "comments": max(thread["messages"] - 1, 0),
                "created_utc": int(thread["ts"]),
                "blurb": truncate_words(thread["blurb"], 220),
            })
            kept += 1
        print(f"[groups.io] {group} ... {len(entries)} messages, "
              f"{kept} threads kept")
        time.sleep(1)
    print(f"[groups.io] total kept: {len(items)} (pre-dedup)")
    return items

# ----------------------------------------------------------------------
# Discourse forums
# ----------------------------------------------------------------------

def collect_discourse():
    items = []
    cutoff = time.time() - DISCOURSE_DAYS_BACK * 86400
    for name, base in DISCOURSE_SITES:
        base = base.rstrip("/")
        data = fetch_json(f"{base}/latest.json?order=created")
        topics = ((data or {}).get("topic_list") or {}).get("topics") or []
        if not topics:
            print(f"[discourse] {name} ... no topics returned "
                  f"(login-only site, or not a Discourse forum?)")
            continue
        site_key = hashlib.md5(base.encode()).hexdigest()[:6]
        kept = 0
        for topic in topics:
            if topic.get("pinned"):
                continue
            ts = parse_iso(topic.get("created_at") or "")
            if not ts or ts < cutoff:
                continue
            replies = topic.get("reply_count")
            if replies is None:
                replies = max((topic.get("posts_count") or 1) - 1, 0)
            views = topic.get("views") or 0
            if replies < DISCOURSE_MIN_REPLIES and views < DISCOURSE_MIN_VIEWS:
                continue
            title = html_lib.unescape((topic.get("title") or "").strip())
            if not title:
                continue
            items.append({
                "id": f"discourse_{site_key}_{topic.get('id')}",
                "source": f"Discourse · {name}",
                "title": title,
                "url": f"{base}/t/{topic.get('slug') or 'topic'}/{topic.get('id')}",
                "score": topic.get("like_count") or 0,
                "comments": replies,
                "views": views,
                "created_utc": int(ts),
                "blurb": truncate_words(strip_html(topic.get("excerpt") or ""), 220),
            })
            kept += 1
        print(f"[discourse] {name} ... {len(topics)} topics, kept {kept}")
        time.sleep(1)
    print(f"[discourse] total kept: {len(items)} (pre-dedup)")
    return items

# ----------------------------------------------------------------------
# Amateur Radio Stack Exchange
# ----------------------------------------------------------------------

def collect_stackexchange():
    items = []
    key = os.environ.get("STACKEXCHANGE_KEY", "")
    fromdate = int(time.time() - STACKEXCHANGE_DAYS_BACK * 86400)
    for site in STACKEXCHANGE_SITES:
        params = {
            "site": site,
            "order": "desc",
            "sort": "creation",
            "fromdate": fromdate,
            "pagesize": STACKEXCHANGE_PAGESIZE,
            "filter": "withbody",
        }
        if key:
            params["key"] = key
        data = fetch_json("https://api.stackexchange.com/2.3/questions?"
                          + urllib.parse.urlencode(params))
        questions = (data or {}).get("items")
        if questions is None:
            print(f"[stackexchange] {site} ... request failed: "
                  f"{(data or {}).get('error_message', 'no data')}")
            continue
        kept = 0
        for q in questions:
            views = q.get("view_count") or 0
            title = html_lib.unescape((q.get("title") or "").strip())
            if views < STACKEXCHANGE_MIN_VIEWS or not title:
                continue
            items.append({
                "id": f"se_{site}_{q.get('question_id')}",
                "source": f"Stack Exchange ({site})",
                "title": title,
                "url": q.get("link") or "",
                "score": q.get("score") or 0,
                "comments": q.get("answer_count") or 0,
                "views": views,
                "created_utc": int(q.get("creation_date") or 0),
                "blurb": truncate_words(strip_html(q.get("body") or ""), 220),
            })
            kept += 1
        print(f"[stackexchange] {site} ... {len(questions)} questions, "
              f"kept {kept}, quota left {(data or {}).get('quota_remaining', '?')}")
        time.sleep(1)
    print(f"[stackexchange] total kept: {len(items)} (pre-dedup)")
    return items

# ----------------------------------------------------------------------
# FCC equipment authorizations
#
# New radios are type-certified here before the manufacturer announces them,
# which makes this a scoop feed rather than a discussion feed: results go to
# the "scoops" block, not into the display rotation.
#
# The OET generic search only returns HTML, so the parse below is deliberately
# tolerant: it treats any table row containing an MM/DD/YYYY date as a grant
# row and picks the FCC ID out of the cells. If a run logs "bytes fetched, no
# grant rows parsed", the query parameters or the result table layout are what
# changed; adjust FCC_SEARCH_URL and the params in collect_fcc.
# ----------------------------------------------------------------------

FCC_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
FCC_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
FCC_HREF_RE = re.compile(r'href\s*=\s*"([^"]+)"', re.IGNORECASE)
FCC_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")
FCC_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{3,17}$")


def collect_fcc():
    if not FCC_ENABLED:
        return []
    results, seen_fcc_ids = [], set()
    now = datetime.now(timezone.utc)
    date_to = now.strftime("%m/%d/%Y")
    date_from = datetime.fromtimestamp(
        now.timestamp() - FCC_DAYS_BACK * 86400,
        tz=timezone.utc).strftime("%m/%d/%Y")
    for applicant in FCC_APPLICANTS:
        params = urllib.parse.urlencode({
            "RequestTimeout": "500",
            "calledFromFrame": "N",
            "applicant_name": applicant,
            "grant_date_from": date_from,
            "grant_date_to": date_to,
        })
        raw = fetch_bytes(f"{FCC_SEARCH_URL}?{params}", label=f"FCC {applicant}")
        if raw is None:
            print(f"[fcc] {applicant} ... request failed")
            continue
        page = raw.decode("utf-8", "replace")
        kept = 0
        for row_html in FCC_ROW_RE.findall(page):
            cells = [strip_html(c) for c in FCC_CELL_RE.findall(row_html)]
            row_text = " ".join(c for c in cells if c)
            granted = FCC_DATE_RE.search(row_text)
            if not granted:
                continue          # header, layout, or navigation row
            fcc_id = next((c.replace(" ", "") for c in cells
                           if FCC_ID_RE.match(c.replace(" ", ""))), "")
            if not fcc_id or fcc_id in seen_fcc_ids:
                continue
            seen_fcc_ids.add(fcc_id)
            href = next((h for h in FCC_HREF_RE.findall(row_html)
                         if "application_id" in h.lower()), "")
            if href.startswith("/"):
                href = "https://apps.fcc.gov" + href
            elif href and not href.lower().startswith("http"):
                href = "https://apps.fcc.gov/oetcf/eas/reports/" + href
            results.append({
                "id": f"fcc_{fcc_id}",
                "applicant": applicant,
                "fcc_id": fcc_id,
                "grant_date": granted.group(0),
                "detail": truncate_words(row_text, 200),
                "url": href or f"{FCC_SEARCH_URL}?{params}",
            })
            kept += 1
            if kept >= FCC_MAX_PER_APPLICANT:
                break
        if kept:
            print(f"[fcc] {applicant} ... {kept} grants")
        else:
            print(f"[fcc] {applicant} ... {len(page)} bytes fetched, "
                  f"no grant rows parsed")
        time.sleep(2)
    print(f"[fcc] total grants: {len(results)}")
    return results

# ----------------------------------------------------------------------
# Trend tracking
# ----------------------------------------------------------------------

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]{1,30}")
URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+\.(?:com|net|org|io)\b",
                    re.IGNORECASE)

def is_distinctive(word, original_title):
    """A single word earns trend eligibility only if it carries specific
    signal: a model/frequency number, an initialism (FT8, APRS, AMSAT),
    or a hyphenated product code (RTL-SDR, IC-7300)."""
    if any(ch.isdigit() for ch in word):
        return True          # 7300, 20m, x-026, ic-7410
    if "-" in word:
        return True          # rtl-sdr, ft-710
    # all-caps initialism in the source title (APRS, AMSAT, POTA, EFHW)
    if re.search(r"\b" + re.escape(word.upper()) + r"\b", original_title):
        if word.upper() in original_title and len(word) <= 6:
            return True
    return False

def extract_terms(title):
    """Pull meaningful terms from a title. Multi-word phrases are the
    primary signal; bare single words qualify only if distinctive.
    Returns a set so each post counts a term at most once."""
    clean = URL_RE.sub(" ", title)
    clean = clean.replace("#", " ").replace("@", " ")
    raw = TOKEN_RE.findall(clean.lower())
    terms = set()

    # single words: must be distinctive AND not a stopword
    for w in raw:
        if w in STOPWORDS or w.isdigit() or len(w) <= 2:
            continue
        if is_distinctive(w, title):
            terms.add(w)

    # bigrams: the workhorse signal; neither half may be a stopword
    for a, b in zip(raw, raw[1:]):
        if a in STOPWORDS or b in STOPWORDS:
            continue
        if a.isdigit() and b.isdigit():
            continue
        if len(a) > 2 or len(b) > 2:
            terms.add(f"{a} {b}")
    return terms


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def update_term_history(fresh_items, today, write=True):
    """Record today's term counts and prune old days."""
    history = load_json(TERM_HISTORY_FILE, {})
    counts = {}
    casing = load_json(TERM_HISTORY_FILE.with_name("term_casing.json"), {})
    for item in fresh_items:
        for term in extract_terms(item["title"]):
            counts[term] = counts.get(term, 0) + 1
            if term not in casing:
                # remember a nice display casing from first sighting
                m = re.search(re.escape(term).replace(r"\ ", r"\s+"),
                              item["title"], re.IGNORECASE)
                casing[term] = m.group(0) if m else term
    history[today] = counts
    # prune old days
    cutoff = (datetime.now(timezone.utc).timestamp()
              - TREND_HISTORY_DAYS * 86400)
    history = {d: c for d, c in history.items()
               if datetime.strptime(d, "%Y-%m-%d")
                  .replace(tzinfo=timezone.utc).timestamp() >= cutoff}
    if write:
        TERM_HISTORY_FILE.write_text(json.dumps(history, indent=2))
        TERM_HISTORY_FILE.with_name("term_casing.json").write_text(
            json.dumps(casing, indent=2))
    return history, casing


def is_stopworded(term):
    """True if every meaningful part of the term is a stopword
    (cleans junk already recorded in term_history.json)."""
    return any(w in STOPWORDS for w in term.split())


def compute_trends(history, casing):
    """Compare the recent window against the baseline window before it."""
    now = datetime.now(timezone.utc)
    recent, baseline = {}, {}
    for day_str, counts in history.items():
        day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age_days = (now - day).total_seconds() / 86400
        bucket = None
        if age_days <= TREND_RECENT_DAYS:
            bucket = recent
        elif age_days <= TREND_RECENT_DAYS + TREND_BASELINE_DAYS:
            bucket = baseline
        if bucket is not None:
            for term, n in counts.items():
                bucket[term] = bucket.get(term, 0) + n
    trends = []
    for term, r in recent.items():
        if r < TREND_MIN_MENTIONS:
            continue
        if is_stopworded(term):
            continue
        b = baseline.get(term, 0)
        # normalize baseline to a per-recent-window rate for fair comparison
        b_rate = b * (TREND_RECENT_DAYS / max(TREND_BASELINE_DAYS, 1))
        velocity = r / (b_rate + 1)
        trends.append({
            "term": casing.get(term, term),
            "recent_mentions": r,
            "baseline_mentions": b,
            "velocity": round(velocity, 2),
            "is_new": b == 0,
        })
    # subsumption: drop a single word when a phrase containing it
    # has (nearly) the same reach; "icom x-026" beats "icom"
    phrases = [t for t in trends if " " in t["term"]]
    def subsumed(t):
        if " " in t["term"]:
            return False
        w = t["term"].lower()
        return any(w in p["term"].lower().split()
                   and p["recent_mentions"] >= t["recent_mentions"] - 1
                   for p in phrases)
    trends = [t for t in trends if not subsumed(t)]

    # prefer multi-word terms when scores tie (more specific = more useful)
    trends.sort(key=lambda t: (t["velocity"], t["recent_mentions"],
                               " " in t["term"]), reverse=True)
    return trends[:TREND_TOP_N]

# ----------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------

def truncate_words(text, max_chars):
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def parse_iso(s):
    try:
        return int(datetime.fromisoformat(
            s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def save_seen(seen):
    cutoff = time.time() - SEEN_RETENTION_DAYS * 86400
    SEEN_FILE.write_text(json.dumps(
        {k: v for k, v in seen.items() if v >= cutoff}, indent=2))

# ----------------------------------------------------------------------
# Ranking and source balancing
# ----------------------------------------------------------------------

def rank_key(item):
    """Comments outrank points. Views are a weak tiebreaker for the sources
    that report them, so a heavily read but unanswered Stack Exchange
    question (exactly the kind of gap worth an episode) can still surface."""
    return (item.get("comments", 0) * 2 + item.get("score", 0)
            + (item.get("views", 0) or 0) / 100.0)


def source_bucket(source):
    """'Mastodon #sota' and 'groups.io · Elecraft' collapse to one bucket each,
    so a chatty feed cannot crowd every other source out of the batch."""
    return source.split(" · ")[0].split(" #")[0].strip() or source


def balance_sources(items):
    """Take the best of every source in turn rather than the global top N."""
    buckets = {}
    for item in items:
        buckets.setdefault(source_bucket(item.get("source", "?")), []).append(item)
    for bucket in buckets.values():
        bucket.sort(key=rank_key, reverse=True)

    picked, depth = [], 0
    while len(picked) < MAX_TOTAL_ITEMS and depth < MAX_PER_SOURCE:
        added = False
        for bucket in buckets.values():
            if depth < len(bucket):
                picked.append(bucket[depth])
                added = True
                if len(picked) >= MAX_TOTAL_ITEMS:
                    break
        if not added:
            break
        depth += 1

    picked.sort(key=rank_key, reverse=True)
    if picked:
        tally = {}
        for item in picked:
            name = source_bucket(item.get("source", "?"))
            tally[name] = tally.get(name, 0) + 1
        print("[balance] " + ", ".join(f"{k}:{v}" for k, v in sorted(tally.items())))
    return picked

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

COLLECTORS = [
    ("reddit", collect_reddit),
    ("hn", collect_hackernews),
    ("mastodon", collect_mastodon),
    ("stackexchange", collect_stackexchange),
    ("groupsio", collect_groups_io),
    ("discourse", collect_discourse),
    ("rss", collect_rss),
]
EXTRA_COLLECTORS = ("youtube", "fcc")


def parse_args(argv):
    """--only=rss,fcc runs just those collectors. It never writes, so a
    single source can be debugged from the Action log without disturbing
    ideas.json, seen.json, or the term history."""
    only = set()
    for arg in argv:
        if arg.startswith("--only="):
            only = {s.strip().lower() for s in arg[7:].split(",") if s.strip()}
        else:
            print(f"[args] ignoring unknown argument: {arg}")
    known = {name for name, _ in COLLECTORS} | set(EXTRA_COLLECTORS)
    for name in sorted(only - known):
        print(f"[args] warning: '{name}' is not a known collector "
              f"({', '.join(sorted(known))})")
    return only


def main():
    print("=== Show Idea Miner: Phase 1 collection run ===")
    only = parse_args(sys.argv[1:])
    dry_run = bool(only)
    if only:
        print(f"[args] running only: {', '.join(sorted(only))} (dry run)")

    seen = load_json(SEEN_FILE, {})
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw = []
    for name, collector in COLLECTORS:
        if only and name not in only:
            continue
        raw.extend(collector())
    competitive = collect_youtube() if not only or "youtube" in only else []
    scoops = collect_fcc() if not only or "fcc" in only else []

    # Dedup within this batch
    by_id = {}
    for item in raw:
        if item["id"] not in by_id and item["title"]:
            by_id[item["id"]] = item
    batch = list(by_id.values())

    # Dedup against previous runs
    fresh = [i for i in batch if i["id"] not in seen]
    print(f"[dedup] {len(batch)} unique this run, {len(fresh)} new vs. history")

    for i in batch:
        seen[i["id"]] = now
    if not dry_run:
        save_seen(seen)

    # Trends: record today's terms, then compute movers
    history, casing = update_term_history(fresh, today, write=not dry_run)
    trends = compute_trends(history, casing)
    if trends:
        print("[trends] top movers: "
              + ", ".join(f"{t['term']} ({t['recent_mentions']})"
                          for t in trends))
    else:
        print("[trends] not enough history yet (needs a few days of runs)")

    # Rank items, giving every source a fair share of the batch
    fresh = balance_sources(fresh)

    # If nothing new came in, keep showing the previous batch
    if not fresh:
        prev = load_json(IDEAS_FILE, {})
        fresh = prev.get("items", [])
        if fresh:
            print("[info] no new items; carrying forward previous batch for display")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": 1,
        "item_count": len(fresh),
        "items": fresh,
        "trends": trends,
        "competitive": competitive,
        "scoops": scoops,
    }

    if dry_run:
        print(f"[dry-run] {len(fresh)} items, {len(trends)} trends, "
              f"{len(competitive)} videos, {len(scoops)} scoops; nothing written")
        return

    if not fresh and not trends:
        print("[warn] empty batch; leaving existing ideas.json untouched")
        return

    IDEAS_FILE.write_text(json.dumps(output, indent=2))
    ARCHIVE_DIR.mkdir(exist_ok=True)
    (ARCHIVE_DIR / f"{today}.json").write_text(json.dumps(output, indent=2))
    print(f"[done] wrote {len(fresh)} items, {len(trends)} trends, "
          f"{len(scoops)} FCC grants")


if __name__ == "__main__":
    main()
