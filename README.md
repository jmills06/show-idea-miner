# show-idea-miner

Mines amateur radio communities for episode ideas for The Everyday Ham, then
asks Claude to turn the raw material into pitches. `index.html` is a rotating
display board that reads the results.

Two GitHub Actions do the work:

| Workflow | Script | Writes |
| --- | --- | --- |
| `mine.yml` (daily) | `scripts/mine_ideas.py` | `ideas.json`, `ideas/YYYY-MM-DD.json`, `seen.json`, `term_history.json` |
| `generate.yml` (weekly) | `scripts/generate_ideas.py` | `show_ideas.json`, `show_ideas/YYYY-MM-DD.json` |

## Sources

Everything is standard library, and every source degrades gracefully: a missing
credential, a dead host, or a renamed feed logs a warning and the run continues
without it.

**Discussion** (these become the display items):

| Source | Notes |
| --- | --- |
| Reddit | Needs `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`. Skipped without them. |
| Hacker News | Algolia search, no key. Low yield in this niche. |
| Mastodon | Hashtag timelines on mastodon.social. |
| Amateur Radio Stack Exchange | No key needed under 300 requests/day. `STACKEXCHANGE_KEY` raises that to 10,000. |
| groups.io | Public list RSS, collapsed so one thread is one item and the reply count is the engagement signal. |
| Discourse forums | `/latest.json` on any Discourse site, no key. |
| Forum / blog RSS | QRZ, Hackaday, and anything else added to `RSS_FEEDS`. |

**Research** (fed to Claude, not shown on the board):

| Source | Notes |
| --- | --- |
| YouTube | Needs `YOUTUBE_API_KEY`. What is performing in the niche right now. |
| FCC equipment authorizations | New radios are type-certified before they are announced, so recent grants are a scoop feed. |

Source lists live in the constants block at the top of `scripts/mine_ideas.py`.
Each collector logs what it fetched and kept, so a group slug or forum URL that
stops working shows up in the Action log and can be pruned from there.

Rather than taking the global top N, the miner takes the best of every source in
turn (`MAX_PER_SOURCE`), so one chatty feed cannot fill the whole batch.

## Running it

```sh
python scripts/mine_ideas.py                    # full run, writes results
python scripts/mine_ideas.py --only=groupsio    # one collector, writes nothing
python scripts/test_collectors.py               # offline checks, no network
```

`--only` accepts `reddit`, `hn`, `mastodon`, `stackexchange`, `groupsio`,
`discourse`, `rss`, `youtube`, `fcc` (comma separated). It never writes, so it
is safe for debugging a single source against the live services.
