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
    # Parse <entry> blocks manually (works with Blogger RSS atom format)
    import re
    # Match each <entry>...</entry> block
    entry_pattern = re.compile(r"<entry>(.*?)</entry>", re.DOTALL | re.IGNORECASE)
    guid_pattern = re.compile(r"<id>(.*?)</id>", re.IGNORECASE)
    title_pattern = re.compile(r"<title>(.*?)</title>", re.IGNORECASE)
    link_pattern = re.compile(r'<link[^>]*href="(.*?)"[^>]*/?>', re.IGNORECASE)
    published_pattern = re.compile(r"<published>(.*?)</published>", re.IGNORECASE)

    for match in entry_pattern.finditer(raw):
        block = match.group(1)
        guid_m = guid_pattern.search(block)
        title_m = title_pattern.search(block)
        link_m = link_pattern.search(block)
        pub_m = published_pattern.search(block)

        guid = html.unescape(guid_m.group(1).strip()) if guid_m else None
        title = html.unescape(title_m.group(1).strip()) if title_m else "Untitled"
        link = link_m.group(1).strip() if link_m else ""
        date = pub_m.group(1).strip() if pub_m else ""

        if guid:
            entries.append({"guid": guid, "title": title, "link": link, "date": date})

    # Sort by publication date descending (newest first)
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def bsky_login(handle: str, app_password: str) -> dict:
    """Authenticate with Bluesky AT Protocol via com.atproto.server.createSession.
    Returns session dict containing accessJwt, did, handle (used for repo field)."""
    url = "https://bsky.social/xrpc/com.atproto.server.createSession"
    body = json.dumps({"identifier": handle, "password": app_password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "post_to_bluesky/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "accessJwt" not in data:
        raise RuntimeError(f"Bluesky login failed: {data.get('error', data)}")
    # Per Bluesky AT Protocol spec, 'did' is the unique ID needed for repo field
    if "did" not in data:
        raise RuntimeError(f"Bluesky login succeeded but did not return 'did': {data}")
    log(f"Logged into Bluesky as {data.get('handle', handle)} (did: {data['did']})")
    return data  # has accessJwt, refreshJwt, handle, did


def bsky_post(access_token: str, did: str, text: str) -> str:
    """Create a Bluesky post via com.atproto.repo.createRecord.
    
    Required by AT Protocol spec: 'repo' must be the user's DID
    (e.g. did:plc:xxxxxxxxxxxxxxxxxxxxx), NOT their handle.
    The DID comes from the createSession response.
    """
    url = "https://bsky.social/xrpc/com.atproto.repo.createRecord"
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now().astimezone().isoformat(),
    }
    body = json.dumps({
        "repo": did,               # ← MUST be DID, not handle (verified via atproto.com docs)
        "collection": "app.bsky.feed.post",
        "record": record,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "post_to_bluesky/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("uri", "unknown")


def truncate_title(title: str, max_len: int = 180) -> str:
    """Truncate title to fit within Bluesky's 300-char limit with room for link."""
    if len(title) <= max_len:
        return title
    return title[:max_len - 3] + "..."


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    log("=== post_to_bluesky.py started ===")

    # 1. Read secrets from env (NEVER from code or config files)
    rss_url = read_env("BLOG_RSS_URL")
    handle = read_env("BLUESKY_HANDLE")
    app_password = read_env("BLUESKY_APP_PASSWORD")

    # 2. Login to Bluesky
    try:
        session = bsky_login(handle, app_password)
        access_token = session["accessJwt"]
    except Exception as e:
        log(f"FATAL: Bluesky login failed — {e}")
        return

    # 3. Fetch RSS feed
    # Extract DID from session (required for repo field in createRecord)
    did = session.get("did")
    if not did:
        log("FATAL: Session missing 'did' — cannot post to Bluesky")
        return

    try:
        entries = fetch_rss(rss_url)
        log(f"RSS feed fetched: {len(entries)} entries found")
    except Exception as e:
        log(f"FATAL: Could not fetch RSS feed — {e}")
        return

    # 4. Load already-posted GUIDs
    posted = load_posted()
    new_entries = [e for e in entries if e["guid"] not in posted]
    log(f"New unpublished entries: {len(new_entries)} (already posted: {len(posted)})")

    if not new_entries:
        log("No new posts to share. Exiting.")
        return

    # 5. Post up to MAX_POSTS_PER_RUN
    posted_count = 0
    for entry in new_entries[:MAX_POSTS_PER_RUN]:
        short_link = entry["link"]
        display_link = short_link if len(short_link) <= 60 else short_link[:57] + "..."
        post_text = truncate_title(entry["title"]) + "\n\n" + display_link

        try:
            uri = bsky_post(access_token, did, post_text)  # uses DID, not handle
            posted.add(entry["guid"])
            posted_count += 1
            log(f"✓ Posted: {entry['title'][:60]} … → {uri}")
        except Exception as e:
            log(f"✗ Failed to post '{entry['title']}': {e}")
            # Continue to next entry — don't crash the workflow

    # 6. Save updated posted.json
    if posted_count > 0:
        save_posted(posted)
        log(f"Saved {posted_count} new GUID(s) to posted.json")
    else:
        log("No new posts were sent — posted.json unchanged")

    log(f"=== Done. Posted {posted_count}/{len(new_entries[:MAX_POSTS_PER_RUN])} entries ===")


if __name__ == "__main__":
    main()
