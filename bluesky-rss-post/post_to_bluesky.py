#!/usr/bin/env python3
"""
post_to_bluesky.py — Post new Blogger blog entries to Bluesky via RSS feed.

Uses ONLY Python standard library (no third-party packages required).
Reads secrets from environment variables only — never hardcodes credentials.

Required env vars:
    BLOG_RSS_URL      — e.g. https://myblog.blogspot.com/feeds/posts/default?alt=rss
    BLUESKY_HANDLE    — your Bluesky handle, e.g. myname.bsky.social
    BLUESKY_APP_PASSWORD  — App Password from bsky.app Settings > Privacy and security > App Passwords

Place this file alongside posted.json (created automatically on first run).
"""

import os
import json
import html
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
posted_file = Path(__file__).parent / "posted.json"
MAX_POSTS_PER_RUN = 2

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


# ── Helpers ─────────────────────────────────────────────────────────────────

def read_env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise ValueError(f"Missing required environment variable: {key}")
    return val


def load_posted() -> set[str]:
    """Load already-posted GUIDs from posted.json (creates empty file if missing)."""
    if posted_file.exists():
        try:
            data = json.loads(posted_file.read_text(encoding="utf-8"))
            return set(data.get("posted_guids", []))
        except (json.JSONDecodeError, KeyError):
            log("WARNING: posted.json malformed — starting fresh")
    return set()


def save_posted(guids: set[str]) -> None:
    posted_file.write_text(
        json.dumps({"posted_guids": sorted(guids), "updated_at": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )


def fetch_rss(url: str) -> list[dict]:
    """Fetch RSS feed and return list of {guid, title, link, date} dicts."""
    req = urllib.request.Request(url, headers={"User-Agent": "post_to_bluesky/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    entries = []
    # Blogger serves two feed formats: Atom (<entry>...</entry>) by default and
    # RSS 2.0 (<item>...</item>) with ?alt=rss. Support both, and tolerate
    # attributes (e.g. <title type='text'>, <guid isPermaLink="false">) and
    # single-quoted link hrefs.
    import re
    # Match each <entry>...</entry> (Atom) or <item>...</item> (RSS 2.0) block
    entry_pattern = re.compile(r"<(?:entry|item)>(.*?)</(?:entry|item)>", re.DOTALL | re.IGNORECASE)
    guid_pattern = re.compile(r"<(?:id|guid)[^>]*>(.*?)</(?:id|guid)>", re.IGNORECASE)
    title_pattern = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE)
    link_tag_pattern = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
    link_href_pattern = re.compile(r"""href=["'](.*?)["']""", re.IGNORECASE)
    link_text_pattern = re.compile(r"<link>(.*?)</link>", re.IGNORECASE)
    published_pattern = re.compile(r"<(?:published|pubDate)>(.*?)</(?:published|pubDate)>", re.IGNORECASE)

    def extract_link(block: str) -> str:
        """Return the article URL: prefer the rel='alternate' link (Atom),
        then any href link, then a plain <link>URL</link> (RSS 2.0)."""
        tags = link_tag_pattern.findall(block)
        for tag in tags:
            if "alternate" in tag.lower():
                href = link_href_pattern.search(tag)
                if href:
                    return href.group(1).strip()
        for tag in tags:
            href = link_href_pattern.search(tag)
            if href:
                return href.group(1).strip()
        text_m = link_text_pattern.search(block)
        return text_m.group(1).strip() if text_m else ""

    for match in entry_pattern.finditer(raw):
        block = match.group(1)
        guid_m = guid_pattern.search(block)
        title_m = title_pattern.search(block)
        pub_m = published_pattern.search(block)

        guid = html.unescape(guid_m.group(1).strip()) if guid_m else None
        title = html.unescape(title_m.group(1).strip()) if title_m else "Untitled"
        link = extract_link(block)
        date = pub_m.group(1).strip() if pub_m else ""

        if guid:
            entries.append({"guid": guid, "title": title, "link": link, "date": date})

    # Sort by publication date descending (newest first)
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def bsky_login(handle: str, app_password: str) -> tuple[str, str]:
    """Log in to Bluesky and return (access_token, did) for use by bsky_post()."""
    url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    payload = json.dumps({"identifier": handle, "password": app_password}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "post_to_bluesky/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Bluesky login failed (HTTP {exc.code}): {detail}") from exc
    access_token = data["accessJwt"]
    did = data["did"]
    log(f"Logged into Bluesky as {data.get('handle', handle)} (did: {did})")
    return access_token, did


def bsky_post(access_token: str, did: str, text: str) -> str:
    """Publish one post. Returns the post's AT-URI."""
    url = "https://bsky.social/xrpc/com.atproto.repo.createRecord"
    payload = json.dumps(
        {
            "repo": did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": datetime.now().astimezone().isoformat().replace("+00:00", "Z"),
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("User-Agent", "post_to_bluesky/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Bluesky post failed (HTTP {exc.code}): {detail}") from exc
    return data["uri"]


def truncate_title(title: str, max_len: int = 180) -> str:
    """Shorten very long titles so the final post stays within Bluesky's 300-char limit."""
    return title if len(title) <= max_len else title[: max_len - 3] + "..."


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    log("=== post_to_bluesky.py started ===")

    # 1. Read secrets from environment (never hardcoded)
    rss_url = read_env("BLOG_RSS_URL")
    handle = read_env("BLUESKY_HANDLE")
    app_password = read_env("BLUESKY_APP_PASSWORD")

    # 2. Log in to Bluesky (uses DID, not handle, for posting)
    access_token, did = bsky_login(handle, app_password)

    # 3. Fetch & parse the RSS feed
    rss_entries = fetch_rss(rss_url)
    log(f"RSS feed fetched: {len(rss_entries)} entries found")

    # 4. Only post entries we haven't posted yet (tracked in posted.json)
    posted = load_posted()
    new_entries = [e for e in rss_entries if e["guid"] not in posted]
    log(f"New unpublished entries: {len(new_entries)} (already posted: {len(posted)})")

    if not new_entries:
        log("No new posts to share. Exiting.")
        return

    # 5. Post up to MAX_POSTS_PER_RUN
    posted_count = 0
    for entry in new_entries[:MAX_POSTS_PER_RUN]:
        # Title + full article link (kept whole so the link stays clickable in the post)
        post_text = truncate_title(entry["title"]) + "\n\n" + entry["link"]

        try:
            uri = bsky_post(access_token, did, post_text)  # uses DID, not handle
            posted.add(entry["guid"])
            posted_count += 1
            log(f"✓ Posted: {entry['title'][:60]} … → {uri}")
        except Exception as exc:  # noqa: BLE001 — log errors, don't kill later posts
            log(f"✗ FAILED to post '{entry['title'][:60]}': {exc}")

    # 6. Save posted.json so the next run doesn't repost the same entries
    if posted_count:
        save_posted(posted)


if __name__ == "__main__":
    main()
