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


def fetch_recent_videos(channel_id: str) -> list[dict]:
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
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            last_error = None
            break
        except urllib.error.HTTPError as e:
            last_error = e
            if attempt < 2:
                print(f"Feed fetch failed ({e}), retrying...", file=sys.stderr)
                time.sleep(10 * (attempt + 1))
    if last_error is not None:
        raise last_error

    root = ET.fromstring(data)
    entries = root.findall("atom:entry", NS)
    if not entries:
        raise RuntimeError("No entries found in feed — check the channel ID.")

    # Feed order is newest first.
    videos = []
    for entry in entries:
        thumb_el = entry.find("media:group/media:thumbnail", NS)
        videos.append({
            "id": entry.find("yt:videoId", NS).text,
            "title": entry.find("atom:title", NS).text,
            "url": entry.find("atom:link", NS).attrib["href"],
            "thumbnail": thumb_el.attrib["url"] if thumb_el is not None else None,
        })
    return videos


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
        headers={
            "Content-Type": "application/json",
            # Discord/Cloudflare rejects the default Python-urllib UA with a 403.
            "User-Agent": "Mozilla/5.0 (compatible; ConsciousLemonBot/1.0)",
        },
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

    videos = fetch_recent_videos(channel_id)
    last_seen = read_last_seen()
    ids = [v["id"] for v in videos]

    if last_seen in ids:
        pending = videos[: ids.index(last_seen)]
    else:
        # No usable state (first run, or last-seen fell out of the feed window): only
        # consider the newest entry rather than re-announcing the whole feed.
        pending = videos[:1] if videos[0]["id"] != last_seen else []

    if not pending:
        print(f"No new upload. Latest is still {videos[0]['id']} ({videos[0]['title']!r}).")
        return 0

    # Walk every upload newer than last-seen, oldest first. Looking only at the newest
    # entry misses a full video when a Short is published after it and before the next
    # run (GOD OF WAR II, 2026-10-12: the Short landed 48 minutes after the video and
    # the next scheduled run was hours later, so the Short masked the real upload).
    for video in reversed(pending):
        try:
            short = is_short(video["id"])
        except Exception as e:
            print(f"Shorts detection failed ({e}); defaulting to treating it as a full video.", file=sys.stderr)
            short = False

        if short:
            print(f"New upload {video['id']} ({video['title']!r}) looks like a Short — skipping announcement.")
        else:
            print(f"New full video detected: {video['id']} ({video['title']!r}). Posting to Discord...")
            post_to_discord(webhook_url, video)  # raises on failure, leaving state untouched so the next run retries
            print("Posted.")

        # Advance state only after this upload is fully handled.
        write_last_seen(video["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
