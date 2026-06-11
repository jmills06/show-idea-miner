#!/usr/bin/env python3
"""
Show Idea Miner - Phase 1 (collection + trend tracking, no AI layer yet)

Sources: Reddit (official OAuth API), Hacker News (Algolia), Mastodon hashtags.
Filters noise, dedupes against previous runs, tracks term trends over time.

Writes:
  - ideas.json            (latest batch + current trends; the display reads this)
  - ideas/YYYY-MM-DD.json (dated archive copy)
  - seen.json             (post IDs already collected, for cross-run dedup)
  - term_history.json     (rolling daily term counts, fuel for trend detection)

Reddit requires repo secrets REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET;
if absent, Reddit is skipped gracefully. Pure standard library, no installs.
"""

import base64
import json
import os
import re
import time
import html as html_lib
import urllib.request
import urllib.parse
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
]
HN_MIN_POINTS = 10
HN_DAYS_BACK = 14

MASTODON_INSTANCE = "https://mastodon.social"
MASTODON_TAGS = ["amateurradio", "hamradio"]
MASTODON_LIMIT = 40          # per tag (API max)
MASTODON_MIN_ENGAGEMENT = 3  # boosts + favorites + replies must reach this

MAX_TOTAL_ITEMS = 60
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

def fetch_json(url, headers=None, data=None):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  [warn] attempt {attempt}/{RETRIES} failed for {url}: {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT)
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
# Trend tracking
# ----------------------------------------------------------------------

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]{1,30}")

def extract_terms(title):
    """Pull meaningful unigrams and bigrams from a title. Returns a set,
    so each post counts a term at most once (no single-post inflation)."""
    words = [w for w in TOKEN_RE.findall(title.lower())
             if w not in STOPWORDS and not w.isdigit() and len(w) > 2]
    terms = set(words)
    raw = TOKEN_RE.findall(title.lower())
    for a, b in zip(raw, raw[1:]):
        if a in STOPWORDS or b in STOPWORDS:
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


def update_term_history(fresh_items, today):
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
    TERM_HISTORY_FILE.write_text(json.dumps(history, indent=2))
    TERM_HISTORY_FILE.with_name("term_casing.json").write_text(
        json.dumps(casing, indent=2))
    return history, casing


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
# Main
# ----------------------------------------------------------------------

def main():
    print("=== Show Idea Miner: Phase 1 collection run ===")
    seen = load_json(SEEN_FILE, {})
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw = collect_reddit() + collect_hackernews() + collect_mastodon()

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
    save_seen(seen)

    # Trends: record today's terms, then compute movers
    history, casing = update_term_history(fresh, today)
    trends = compute_trends(history, casing)
    if trends:
        print("[trends] top movers: "
              + ", ".join(f"{t['term']} ({t['recent_mentions']})"
                          for t in trends))
    else:
        print("[trends] not enough history yet (needs a few days of runs)")

    # Rank items: comments weighted over score
    fresh.sort(key=lambda i: i["comments"] * 2 + i["score"], reverse=True)
    fresh = fresh[:MAX_TOTAL_ITEMS]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": 1,
        "item_count": len(fresh),
        "items": fresh,
        "trends": trends,
    }

    if not fresh and not trends:
        print("[warn] empty batch; leaving existing ideas.json untouched")
        return

    IDEAS_FILE.write_text(json.dumps(output, indent=2))
    ARCHIVE_DIR.mkdir(exist_ok=True)
    (ARCHIVE_DIR / f"{today}.json").write_text(json.dumps(output, indent=2))
    print(f"[done] wrote {len(fresh)} items and {len(trends)} trends")


if __name__ == "__main__":
    main()
