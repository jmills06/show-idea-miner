#!/usr/bin/env python3
"""
Offline checks for the collectors in mine_ideas.py.

Every collector runs against a fixture payload instead of a live request, so
this needs no credentials and never touches the network. It covers the parsing
and selection logic, not whether any given host is reachable.

    python scripts/test_collectors.py

Exits non-zero if a check fails.
"""

import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MINER = Path(__file__).resolve().parent / "mine_ideas.py"
NOW = time.time()
FAILURES = []


def load_miner():
    """Load mine_ideas.py into a private namespace we can monkeypatch."""
    ns = {"__file__": str(MINER), "__name__": "mine_ideas_fixture"}
    exec(compile(MINER.read_text(), str(MINER), "exec"), ns)
    ns["time"].sleep = lambda seconds: None      # no waiting between fixtures
    return ns


def check(name, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + name
          + ("" if condition else f"   <- {detail}"))
    if not condition:
        FAILURES.append(name)


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def rfc2822(ts):
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(ts))


def stub_bytes(ns, payload):
    ns["fetch_bytes"] = lambda url, headers=None, data=None, label=None: payload


def stub_json(ns, payload):
    ns["fetch_json"] = lambda url, headers=None, data=None: payload


# ----------------------------------------------------------------------
# Feed parsing (RSS 2.0 and Atom)
# ----------------------------------------------------------------------

def test_feed_parsing(ns):
    rss = f"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>Hackaday &amp; SDR</title><link>https://example.com/a</link>
    <description>&lt;p&gt;Body here&lt;/p&gt;The post Foo appeared first on Hackaday.</description>
    <pubDate>{rfc2822(NOW - 3600)}</pubDate></item>
    </channel></rss>""".encode()
    atom = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><title>Atom entry</title><link rel="alternate" href="https://example.com/b"/>
    <summary>atom summary</summary><published>{iso(NOW - 7200)}</published></entry>
    </feed>""".encode()

    r = list(ns["parse_feed"](rss))
    a = list(ns["parse_feed"](atom))
    check("RSS entry parsed",
          len(r) == 1 and r[0][0] == "Hackaday & SDR" and r[0][1] == "https://example.com/a", r)
    check("RSS pubDate parsed", abs(r[0][3] - (NOW - 3600)) < 5, r[0][3])
    check("Atom entry parsed", len(a) == 1 and a[0][1] == "https://example.com/b", a)
    check("Atom published parsed", abs(a[0][3] - (NOW - 7200)) < 5, a[0][3])

    ns["RSS_FEEDS"] = [("Hackaday", "https://example.com/feed")]
    stub_bytes(ns, rss)
    items = ns["collect_rss"]()
    check("collect_rss maps an item", len(items) == 1 and items[0]["source"] == "Hackaday", items)
    check("WordPress boilerplate stripped",
          "appeared first on" not in items[0]["blurb"], items[0]["blurb"])


# ----------------------------------------------------------------------
# groups.io thread collapsing
# ----------------------------------------------------------------------

def test_groups_io(ns):
    for raw, want in [
        ("[Elecraft] Re: KX3 noise", "KX3 noise"),
        ("Re: [Elecraft] KX3 noise", "KX3 noise"),
        ("Re: Re: KX3 noise", "KX3 noise"),
        ("KX3 noise", "KX3 noise"),
        ("Reflector antenna question", "Reflector antenna question"),
    ]:
        got = ns["thread_subject"](raw)
        check(f"thread_subject({raw!r})", got == want, got)

    titles = ["KX3 noise floor"] + ["Re: KX3 noise floor"] * 6 + ["[Elecraft] Antenna tuner"]
    messages = "".join(
        f"<item><title>{title}</title>"
        f"<link>https://groups.io/g/Elecraft/message/{i}</link>"
        f"<description>message {i}</description>"
        f"<pubDate>{rfc2822(NOW - (100 - i) * 60)}</pubDate></item>"
        for i, title in enumerate(titles))
    feed = f'<?xml version="1.0"?><rss version="2.0"><channel>{messages}</channel></rss>'

    ns["GROUPS_IO_GROUPS"] = ["Elecraft"]
    stub_bytes(ns, feed.encode())
    threads = sorted(ns["collect_groups_io"](), key=lambda i: -i["comments"])
    check("seven messages collapse to one thread", len(threads) == 2,
          [t["title"] for t in threads])
    check("reply count becomes the engagement signal",
          threads[0]["comments"] == 6 and threads[0]["score"] == 7, threads[0])
    check("thread opener is the linked message",
          threads[0]["url"].endswith("/message/0"), threads[0]["url"])
    check("id carries an activity bucket so busy threads resurface",
          threads[0]["id"].endswith("_1") and threads[1]["id"].endswith("_0"),
          [t["id"] for t in threads])
    check("every list shares one balancing bucket",
          ns["source_bucket"](threads[0]["source"]) == "groups.io", threads[0]["source"])


# ----------------------------------------------------------------------
# Discourse
# ----------------------------------------------------------------------

def test_discourse(ns):
    payload = {"topic_list": {"topics": [
        {"id": 11, "slug": "antenna-help", "title": "Antenna &amp; feedline help",
         "reply_count": 9, "views": 420, "like_count": 4,
         "created_at": iso(NOW - 86400), "excerpt": "<p>hi</p>"},
        {"id": 12, "slug": "welcome", "title": "Welcome", "reply_count": 50,
         "views": 9999, "pinned": True, "created_at": iso(NOW - 86400)},
        {"id": 13, "slug": "quiet", "title": "Quiet topic", "reply_count": 0,
         "views": 5, "created_at": iso(NOW - 86400)},
        {"id": 14, "slug": "stale", "title": "Old topic", "reply_count": 40,
         "views": 900, "created_at": iso(NOW - 60 * 86400)},
        {"id": 15, "slug": "popular-unanswered", "title": "Nobody answered this",
         "reply_count": 0, "views": 800, "created_at": iso(NOW - 86400)},
    ]}}
    ns["DISCOURSE_SITES"] = [("GNU Radio", "https://discourse.gnuradio.org/")]
    stub_json(ns, payload)
    topics = ns["collect_discourse"]()
    titles = sorted(t["title"] for t in topics)
    check("engaged topics kept, entities unescaped",
          titles == ["Antenna & feedline help", "Nobody answered this"], titles)
    check("pinned, quiet, and stale topics dropped", len(topics) == 2, titles)
    check("topic url built from slug and id",
          any(t["url"] == "https://discourse.gnuradio.org/t/antenna-help/11" for t in topics),
          [t["url"] for t in topics])


# ----------------------------------------------------------------------
# Stack Exchange (also exercises the gzip transport)
# ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_stackexchange(ns):
    payload = {"quota_remaining": 287, "items": [
        {"question_id": 1, "title": "Why does my &quot;Baofeng&quot; drift?",
         "link": "https://ham.stackexchange.com/q/1", "score": 7, "answer_count": 2,
         "view_count": 900, "creation_date": int(NOW - 3600), "body": "<p>It drifts</p>"},
        {"question_id": 2, "title": "Ignored question",
         "link": "https://ham.stackexchange.com/q/2", "score": 0, "answer_count": 0,
         "view_count": 4, "creation_date": int(NOW - 3600), "body": "x"},
    ]}
    gzipped = gzip.compress(json.dumps(payload).encode())
    # Stack Exchange always gzips; go through the real fetch_bytes to prove
    # the transport decompresses it.
    ns["urllib"].request.urlopen = lambda req, timeout=None: FakeResponse(gzipped)
    ns["STACKEXCHANGE_SITES"] = ["ham"]
    questions = ns["collect_stackexchange"]()
    check("gzipped response decoded", len(questions) == 1, questions)
    check("html entities unescaped in titles",
          questions and questions[0]["title"] == 'Why does my "Baofeng" drift?',
          questions and questions[0]["title"])
    check("question below the view floor dropped",
          all(q["id"] != "se_ham_2" for q in questions), questions)
    check("views stored so unanswered questions can still rank",
          questions and questions[0].get("views") == 900, questions)


# ----------------------------------------------------------------------
# FCC equipment authorizations
# ----------------------------------------------------------------------

def test_fcc(ns):
    page = """<html><body><table>
    <tr><th>Grantee</th><th>FCC ID</th><th>Grant Date</th></tr>
    <tr><td>Icom Incorporated</td><td>AFJ339500</td><td>08/12/2026</td>
        <td><a href="ViewExhibitReport.cfm?mode=Exhibits&amp;application_id=abc123">Detail</a></td></tr>
    <tr><td>Icom Incorporated</td><td>AFJ339500</td><td>08/12/2026</td></tr>
    <tr><td>navigation row with no date</td></tr>
    </table></body></html>"""
    ns["FCC_APPLICANTS"] = ["Icom"]
    stub_bytes(ns, page.encode())
    grants = ns["collect_fcc"]()
    check("grant row parsed, duplicate id skipped", len(grants) == 1, grants)
    check("fcc id extracted", grants and grants[0]["fcc_id"] == "AFJ339500", grants)
    check("grant date extracted", grants and grants[0]["grant_date"] == "08/12/2026", grants)
    check("detail link made absolute",
          grants and grants[0]["url"].startswith(
              "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport"),
          grants and grants[0]["url"])

    stub_bytes(ns, b"<html>no table here</html>")
    check("unparseable page returns nothing instead of raising",
          ns["collect_fcc"]() == [], "raised or returned rows")


# ----------------------------------------------------------------------
# Ranking and source balancing
# ----------------------------------------------------------------------

def test_balancing(ns):
    def item(source, comments):
        return {"id": f"{source}{comments}", "source": source, "title": "t",
                "comments": comments, "score": 0}

    pool = ([item("Mastodon #hamradio", 50 - i) for i in range(40)]
            + [item("groups.io · Elecraft", 3) for _ in range(5)]
            + [item("Stack Exchange (ham)", 1) for _ in range(4)]
            + [item("QRZ Forums", 0) for _ in range(10)])
    ns["MAX_TOTAL_ITEMS"], ns["MAX_PER_SOURCE"] = 20, 18
    picked = ns["balance_sources"](pool)

    tally = {}
    for entry in picked:
        bucket = ns["source_bucket"](entry["source"])
        tally[bucket] = tally.get(bucket, 0) + 1
    check("loudest source cannot take the whole batch", tally.get("Mastodon", 0) <= 8, tally)
    check("zero-engagement sources still get in", tally.get("QRZ Forums", 0) >= 4, tally)
    check("total cap respected", len(picked) == 20, len(picked))
    check("views act as a ranking tiebreaker",
          ns["rank_key"]({"comments": 0, "score": 0, "views": 800})
          > ns["rank_key"]({"comments": 1, "score": 0}), "views ignored")


def main():
    for test in (test_feed_parsing, test_groups_io, test_discourse,
                 test_stackexchange, test_fcc, test_balancing):
        test(load_miner())      # a fresh namespace per test, no cross-talk
    print()
    print("FAILURES:", ", ".join(FAILURES) if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
