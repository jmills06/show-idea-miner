#!/usr/bin/env python3
"""
Show Idea Miner - Phase 2 (Claude idea generation)

Reads the past week of mined items from ideas/ plus current trends,
sends them to the Claude API, and produces refined episode pitches.

Writes:
  - show_ideas.json            (latest pitch batch; the display reads this)
  - show_ideas/YYYY-MM-DD.json (dated archive copy)

Requires repo secret ANTHROPIC_API_KEY (passed as an env var by the
GitHub Action). Pure standard library, no installs.
"""

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ----------------------------------------------------------------------
# Working constants (tune here, not deep in the code)
# ----------------------------------------------------------------------

MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 3000
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

LOOKBACK_DAYS = 7         # how much mined material to feed Claude
MAX_ITEMS_TO_SEND = 120   # cap the raw material (keeps cost trivial)
IDEAS_REQUESTED = "5 to 8"
DEDUP_BATCHES = 3         # past pitch batches shown to Claude as "don't repeat"

REQUEST_TIMEOUT = 120
RETRIES = 3
RETRY_WAIT = 15

ROOT = Path(__file__).resolve().parent.parent
IDEAS_FILE = ROOT / "ideas.json"
ARCHIVE_DIR = ROOT / "ideas"
SHOW_IDEAS_FILE = ROOT / "show_ideas.json"
SHOW_ARCHIVE_DIR = ROOT / "show_ideas"

SYSTEM_PROMPT = """You are the producer for The Everyday Ham, a podcast and
YouTube channel for everyday amateur radio operators. The hosts cover portable
operations, POTA, digital modes, gear, club life, and practical on-air skills,
with an accessible, enthusiastic, non-elitist tone.

You will receive trending posts and discussions collected from amateur radio
communities over the past week, plus a list of terms currently trending upward.

Identify the {ideas_requested} strongest potential episode topics. Favor:
- Topics generating real discussion (high comment counts) over mere popularity
- Timely hooks (new gear, events, controversies, rule changes)
- Topics where The Everyday Ham can add a practical, relatable angle
- Variety across the batch (don't make every idea about the same theme)

Avoid repeating ideas from previous batches unless there is a genuinely new
angle, in which case say what's new.

Style rules: never use em dashes (the long dash) anywhere in your output.
Use a period, comma, colon, or parentheses instead. Keep titles punchy and
plainspoken, not clickbaity.

Respond with ONLY valid JSON, no markdown fences, no preamble, matching:
{{
  "ideas": [
    {{
      "title": "Working episode title, punchy, max ~70 chars",
      "why_now": "One sentence on why this is timely, max ~140 chars",
      "angle": "The Everyday Ham's take in 1-2 sentences, max ~200 chars",
      "sources": [{{"label": "r/amateurradio, 340 comments", "url": "https://..."}}]
    }}
  ]
}}
Each idea needs 1-3 sources drawn from the provided material, using their
real URLs. Output JSON only."""

# ----------------------------------------------------------------------
# Gather material
# ----------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def gather_week_of_items():
    """Merge unique items from the past LOOKBACK_DAYS of archive files."""
    cutoff = datetime.now(timezone.utc).timestamp() - LOOKBACK_DAYS * 86400
    by_id = {}
    if ARCHIVE_DIR.exists():
        for f in sorted(ARCHIVE_DIR.glob("*.json")):
            try:
                day = datetime.strptime(f.stem, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            if day.timestamp() < cutoff - 86400:
                continue
            for item in load_json(f, {}).get("items", []):
                if item.get("id") and item.get("title"):
                    by_id[item["id"]] = item
    items = list(by_id.values())
    items.sort(key=lambda i: (i.get("comments", 0) * 2 + i.get("score", 0)),
               reverse=True)
    return items[:MAX_ITEMS_TO_SEND]


def gather_previous_titles():
    """Titles from recent pitch batches, so Claude avoids repeats."""
    titles = []
    if SHOW_ARCHIVE_DIR.exists():
        for f in sorted(SHOW_ARCHIVE_DIR.glob("*.json"))[-DEDUP_BATCHES:]:
            for idea in load_json(f, {}).get("ideas", []):
                if idea.get("title"):
                    titles.append(idea["title"])
    return titles


def build_user_prompt(items, trends, previous_titles, competitive):
    lines = ["## Trending terms this week (term: recent mentions, velocity)"]
    if trends:
        for t in trends:
            flag = " [NEW]" if t.get("is_new") else ""
            lines.append(f"- {t['term']}: {t['recent_mentions']} mentions, "
                         f"velocity {t['velocity']}{flag}")
    else:
        lines.append("(no trend data yet)")

    if competitive:
        lines.append("\n## What's performing on YouTube in this niche right now"
                     "\n(competitive research: proven topics, but consider a"
                     " differentiated angle rather than copying)")
        for v in competitive[:15]:
            lines.append(f"- \"{v.get('title','')}\" by {v.get('channel','?')}: "
                         f"{v.get('views',0):,} views, "
                         f"{v.get('comments',0):,} comments")

    if previous_titles:
        lines.append("\n## Ideas already pitched in recent batches (avoid repeats)")
        for t in previous_titles:
            lines.append(f"- {t}")

    lines.append(f"\n## Collected material from the past {LOOKBACK_DAYS} days "
                 f"({len(items)} items)")
    for i, item in enumerate(items, 1):
        meta = (f"[{item.get('source','?')}] {item.get('score',0)} pts, "
                f"{item.get('comments',0)} comments")
        lines.append(f"{i}. {item.get('title','').strip()}")
        lines.append(f"   {meta} | {item.get('url','')}")
        blurb = (item.get("blurb") or "").strip()
        if blurb and blurb != item.get("title", "").strip():
            lines.append(f"   > {blurb}")
    return "\n".join(lines)

# ----------------------------------------------------------------------
# Claude API call
# ----------------------------------------------------------------------

def call_claude(system_prompt, user_prompt):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[claude] ERROR: ANTHROPIC_API_KEY not set")
        return None
    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = "".join(block.get("text", "")
                           for block in data.get("content", [])
                           if block.get("type") == "text")
            usage = data.get("usage", {})
            print(f"[claude] ok: {usage.get('input_tokens','?')} in / "
                  f"{usage.get('output_tokens','?')} out tokens")
            return text
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:400]
            except Exception:
                pass
            print(f"[claude] attempt {attempt}/{RETRIES} HTTP {e.code}: {detail}")
        except Exception as e:
            print(f"[claude] attempt {attempt}/{RETRIES} failed: {e}")
        if attempt < RETRIES:
            time.sleep(RETRY_WAIT)
    return None


def scrub_dashes(text):
    """Belt-and-suspenders: remove em/en dashes even if the model emits one.
    ' word — word ' becomes ' word, word '; tight 'a—b' becomes 'a, b'."""
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)
    return text


def parse_ideas(text):
    """Parse Claude's response defensively; strip fences if present."""
    if not text:
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    # fall back to the outermost braces if there's stray preamble
    if not cleaned.lstrip().startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            return []
        cleaned = cleaned[start:end + 1]
    try:
        data = json.loads(cleaned)
    except Exception as e:
        print(f"[parse] ERROR: could not parse response as JSON: {e}")
        return []
    ideas = []
    for raw in data.get("ideas", []):
        if not raw.get("title"):
            continue
        ideas.append({
            "title": scrub_dashes(str(raw.get("title", "")).strip()),
            "why_now": scrub_dashes(str(raw.get("why_now", "")).strip()),
            "angle": scrub_dashes(str(raw.get("angle", "")).strip()),
            "sources": [
                {"label": str(s.get("label", "")).strip(),
                 "url": str(s.get("url", "")).strip()}
                for s in raw.get("sources", []) if isinstance(s, dict)
            ][:3],
        })
    return ideas

# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    print("=== Show Idea Miner: Phase 2 generation run ===")
    items = gather_week_of_items()
    latest = load_json(IDEAS_FILE, {})
    trends = latest.get("trends", [])
    competitive = latest.get("competitive", [])
    previous_titles = gather_previous_titles()
    print(f"[gather] {len(items)} items, {len(trends)} trends, "
          f"{len(competitive)} competitive videos, "
          f"{len(previous_titles)} previous titles")

    if not items:
        print("[warn] no mined material found; nothing to generate from")
        return

    system_prompt = SYSTEM_PROMPT.format(ideas_requested=IDEAS_REQUESTED)
    user_prompt = build_user_prompt(items, trends, previous_titles, competitive)
    text = call_claude(system_prompt, user_prompt)
    ideas = parse_ideas(text)

    if not ideas:
        print("[warn] no usable ideas returned; leaving show_ideas.json untouched")
        return

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "idea_count": len(ideas),
        "ideas": ideas,
    }
    SHOW_IDEAS_FILE.write_text(json.dumps(output, indent=2))
    SHOW_ARCHIVE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (SHOW_ARCHIVE_DIR / f"{stamp}.json").write_text(json.dumps(output, indent=2))
    print(f"[done] wrote {len(ideas)} episode pitches")
    for idea in ideas:
        print(f"  - {idea['title']}")


if __name__ == "__main__":
    main()
