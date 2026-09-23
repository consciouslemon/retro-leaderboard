#!/usr/bin/env python3
"""Poll a YouTube channel's public RSS feed and post to a Discord webhook when a new
FULL-LENGTH video appears (Shorts are detected and skipped).

Free, self-hosted: no paid API keys or third-party services. Reads the channel ID and
Discord webhook URL from environment variables (set as GitHub Actions secrets), and
tracks the last-seen video ID in a state file committed back to the repo.

Shorts detection: YouTube's RSS feed has no duration field, so there's no direct flag
to read. Instead we rely on a known behavior: requesting https://www.youtube.com/shorts/{id}
returns 200 for an actual Short, but redirects (3xx) to the normal watch page for a
regular video. This is a heuristic based on observed YouTube behavior, not a documented
API contract — if YouTube changes this behavior, detection could silently break, so it's
worth spot-checking the first few times this fires. If detection fails outright (network
error), we default to treating it as a full video and posting anyway, since silently
missing a real upload announcement is worse than an occasional false trigger on a Short.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

STATE_FILE = os.path.join(os.path.dirname(__file__), "last_video.txt")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_latest_video(channel_id: str) -> dict:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ConsciousLemonBot/1.0)"},
    )

    # The feed endpoint occasionally returns a transient error (observed: one HTTP 404
    # in 9 runs, which cleared on the very next scheduled run with no code change) —
    # retry once with a short backoff before giving up, so a one-off blip doesn't need
    # to wait for the next 20-minute cron tick (and doesn't send a failure email) to
    # resolve itself.
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            last_error = None
            break
        except urllib.error.HTTPError as e:
            last_error = e
            if attempt == 0:
                print(f"Feed fetch failed ({e}), retrying once...", file=sys.stderr)
                time.sleep(5)
    if last_error is not None:
        raise last_error

    root = ET.fromstring(data)
    entry = root.find("atom:entry", NS)
    if entry is None:
        raise RuntimeError("No entries found in feed — check the channel ID.")

    video_id = entry.find("yt:videoId", NS).text
    title = entry.find("atom:title", NS).text
    link = entry.find("atom:link", NS).attrib["href"]
    thumb_el = entry.find("media:group/media:thumbnail", NS)
    thumbnail = thumb_el.attrib["url"] if thumb_el is not None else None

    return {"id": video_id, "title": title, "url": link, "thumbnail": thumbnail}


def is_short(video_id: str) -> bool:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{video_id}",
        method="HEAD",
        headers={"User-Agent": "Mozilla/5.0 (compatible; ConsciousLemonBot/1.0)"},
    )
    try:
        resp = opener.open(req, timeout=10)
        return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return False
        raise


def read_last_seen() -> str | None:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    return None


def write_last_seen(video_id: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(video_id)


def post_to_discord(webhook_url: str, video: dict) -> None:
    payload = {
        "content": "📺 New video is live!",
        "embeds": [
            {
                "title": video["title"],
                "url": video["url"],
                "image": {"url": video["thumbnail"]} if video["thumbnail"] else None,
                "color": 0xFFD400,
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 204):
            raise RuntimeError(f"Discord webhook returned status {resp.status}")


def main() -> int:
    channel_id = os.environ.get("YOUTUBE_CHANNEL_ID")
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if not channel_id or not webhook_url:
        print("Missing YOUTUBE_CHANNEL_ID or DISCORD_WEBHOOK_URL env vars.", file=sys.stderr)
        return 1

    latest = fetch_latest_video(channel_id)
    last_seen = read_last_seen()

    if latest["id"] == last_seen:
        print(f"No new upload. Latest is still {latest['id']} ({latest['title']!r}).")
        return 0

    # Mark as processed immediately regardless of outcome, so a Short doesn't get
    # re-checked on every run until the next real upload comes along.
    write_last_seen(latest["id"])

    try:
        short = is_short(latest["id"])
    except Exception as e:
        print(f"Shorts detection failed ({e}); defaulting to treating it as a full video.", file=sys.stderr)
        short = False

    if short:
        print(f"New upload {latest['id']} ({latest['title']!r}) looks like a Short — skipping announcement.")
        return 0

    print(f"New full video detected: {latest['id']} ({latest['title']!r}). Posting to Discord...")
    post_to_discord(webhook_url, latest)
    print("Posted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
