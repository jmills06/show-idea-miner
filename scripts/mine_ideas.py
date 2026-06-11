#!/usr/bin/env python3
"""
Show Idea Miner - Phase 1 (collection only, no AI layer yet)

Pulls trending posts from Reddit and Hacker News, filters noise,
dedupes against previous runs, and writes:
  - ideas.json            (latest batch, the display reads this)
  - ideas/YYYY-MM-DD.json (dated archive copy)
  - seen.json             (IDs already collected, for cross-run dedup)

Uses only the Python standard library. No pip installs needed.
"""

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------
# Working constants (tune here, not deep in the code)
# ----------------------------------------------------------------------

USER_AGENT = "EverydayHamIdeaMiner/1.0 (by K8JKU; podcast research tool)"

SUBREDDITS = ["amateurradio", "hamradio", "morse", "RTLSDR"]
REDDIT_SORT = "top"          # top posts...
REDDIT_TIMEFRAME = "week"    # ...of the past week
REDDIT_LIMIT = 25            # per subreddit
REDDIT_MIN_SCORE = 25        # ignore posts below this many upvotes
REDDIT_MIN_COMMENTS = 10     # OR below this many comments (must pass one)

# Quoted phrases only. Deliberately NO bare "RF" or "antenna",
# those pull networking and EE noise on HN.
HN_QUERIES = [
    "ham radio",
    "amateur radio",
    "software defined radio",
    "shortwave",
    "Meshtastic",
    "LoRa radio",
]
HN_MIN_POINTS = 20           # ignore HN stories below this score
HN_DAYS_BACK = 7             # only stories from the past week

MAX_TOTAL_ITEMS = 60         # cap the final batch size
SEEN_RETENTION_DAYS = 45     # forget seen IDs older than this
REQUEST_TIMEOUT = 20         # seconds
RETRIES = 3
RETRY_WAIT = 5               # seconds between retries

# Repo root is one level up from scripts/
ROOT = Path(__file__).resolve().parent.parent
IDEAS_FILE = ROOT / "ideas.json"
ARCHIVE_DIR = ROOT / "ideas"
SEEN_FILE = ROOT / "seen.json"

# ----------------------------------------------------------------------
# HTTP helper with retries
# ----------------------------------------------------------------------

def fetch_json(url):
    """GET a URL and parse JSON, with retries. Returns None on failure."""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  [warn] attempt {attempt}/{RETRIES} failed for {url}: {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT)
    return None

# ----------------------------------------------------------------------
# Collectors (each returns a list of normalized item dicts)
# ----------------------------------------------------------------------

def collect_reddit():
    items = []
    for sub in SUBREDDITS:
        url = (
            f"https://www.reddit.com/r/{sub}/{REDDIT_SORT}.json"
            f"?t={REDDIT_TIMEFRAME}&limit={REDDIT_LIMIT}"
        )
        print(f"[reddit] r/{sub} ...")
        data = fetch_json(url)
        if not data:
            print(f"  [warn] skipping r/{sub}, no data")
            continue
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
        time.sleep(2)  # be polite between subreddit requests
    print(f"[reddit] kept {len(items)} items")
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
        url = f"https://hn.algolia.com/api/v1/search?{params}"
        print(f"[hn] query: {q} ...")
        data = fetch_json(url)
        if not data:
            continue
        for hit in data.get("hits", []):
            items.append({
                "id": f"hn_{hit.get('objectID')}",
                "source": "Hacker News",
                "title": (hit.get("title") or "").strip(),
                "url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "score": hit.get("points", 0),
                "comments": hit.get("num_comments", 0),
                "created_utc": hit.get("created_at_i", 0),
                "blurb": "",
            })
        time.sleep(1)
    print(f"[hn] kept {len(items)} items (pre-dedup)")
    return items

# ----------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------

def truncate_words(text, max_chars):
    """Truncate at a word boundary, never mid-word."""
    text = " ".join(text.split())  # collapse whitespace/newlines
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0]
    return cut + "…"


def load_seen():
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except Exception:
            pass
    return {}


def save_seen(seen):
    cutoff = time.time() - SEEN_RETENTION_DAYS * 86400
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.write_text(json.dumps(pruned, indent=2))

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    print("=== Show Idea Miner: Phase 1 collection run ===")
    seen = load_seen()
    now = time.time()

    raw = collect_reddit() + collect_hackernews()

    # Dedup within this batch (same story can hit multiple HN queries)
    by_id = {}
    for item in raw:
        if item["id"] not in by_id and item["title"]:
            by_id[item["id"]] = item
    batch = list(by_id.values())

    # Dedup against previous runs
    fresh = [i for i in batch if i["id"] not in seen]
    print(f"[dedup] {len(batch)} unique this run, {len(fresh)} new vs. history")

    # Mark everything from this run as seen
    for i in batch:
        seen[i["id"]] = now
    save_seen(seen)

    # Rank: comments weighted slightly over score (discussion = episode fuel)
    fresh.sort(key=lambda i: i["comments"] * 2 + i["score"], reverse=True)
    fresh = fresh[:MAX_TOTAL_ITEMS]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": 1,
        "item_count": len(fresh),
        "items": fresh,
    }

    # Safety rule: never overwrite good data with an empty batch
    if not fresh:
        print("[warn] empty batch; leaving existing ideas.json untouched")
        return

    IDEAS_FILE.write_text(json.dumps(output, indent=2))
    ARCHIVE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (ARCHIVE_DIR / f"{stamp}.json").write_text(json.dumps(output, indent=2))
    print(f"[done] wrote {len(fresh)} items to ideas.json and ideas/{stamp}.json")


if __name__ == "__main__":
    main()
