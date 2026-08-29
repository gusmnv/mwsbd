"""
Mr Wall Street — #market-news bot
Reads financial RSS feeds and posts fresh headlines to a Discord webhook.
Runs on GitHub Actions (see .github/workflows/market-news.yml).

Required env vars:
  DISCORD_WEBHOOK_MARKET_NEWS  — Discord webhook URL for the #market-news channel
"""
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

FEEDS = [
    # CNBC — Markets
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    # MarketWatch — Top Stories
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

MAX_POSTS_PER_RUN = 5          # never spam the channel
SEEN_FILE = Path(__file__).resolve().parent.parent / "state" / "seen_news.json"

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_MARKET_NEWS", "").strip()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (MrWallStreetBot)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parse_rss(xml_bytes: bytes):
    """Yield (title, link) from an RSS feed."""
    root = ElementTree.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            yield title, link


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # keep the file bounded
    SEEN_FILE.write_text(json.dumps(sorted(seen)[-500:]))


def post_to_discord(lines: list[str]):
    payload = {
        "username": "Mr Wall Street — Markets",
        "content": "\n".join(lines),
        "allowed_mentions": {"parse": []},
    }
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "MrWallStreetBot"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status


def main():
    if not WEBHOOK:
        sys.exit("Missing DISCORD_WEBHOOK_MARKET_NEWS env var")

    seen = load_seen()
    fresh = []

    for source, url in FEEDS:
        try:
            for title, link in parse_rss(fetch(url)):
                key = hashlib.sha1(title.lower().encode()).hexdigest()
                if key not in seen:
                    seen.add(key)
                    fresh.append(f"**{source}** · [{title}](<{link}>)")
        except Exception as e:
            print(f"[warn] {source} feed failed: {e}")

    if not fresh:
        print("No new headlines.")
        save_seen(seen)
        return

    batch = fresh[:MAX_POSTS_PER_RUN]
    header = "📰 **Market News**"
    status = post_to_discord([header] + batch)
    print(f"Posted {len(batch)} headlines (HTTP {status}).")
    save_seen(seen)


if __name__ == "__main__":
    main()
