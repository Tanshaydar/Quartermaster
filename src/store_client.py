"""
VaultMCP store client: interactive login, library fetching, and metadata
enrichment for Unity Asset Store and Epic Games Fab.

Login model
-----------
There is no public OAuth API for either store's purchase library, so we use a
browser-assisted login flow (the standard pattern for community tools):

  1. `interactive_login(provider)` opens a VISIBLE Chromium window.
  2. The user signs in manually (2FA / captcha / SSO all work naturally).
  3. We detect success and persist the session into a per-provider browser
     profile directory (`profiles/<provider>/`).
  4. Later refreshes reuse that profile headlessly; if the session expires the
     UI prompts the user to log in again.

Library fetching strategy
-------------------------
Instead of hardcoding private GraphQL endpoints (which change often), we open
the store's "my assets / library" page with the saved session and INTERCEPT all
JSON network responses, harvesting any payload that looks like an asset list.
This is resilient to endpoint changes on both stores.

Media policy
------------
- Images: cached to disk via the local proxy endpoint in server.py
          (config: media_cache_enabled).
- Videos: links only (YouTube trailers / store-hosted videos are stored as
          URLs, never downloaded). config: video_mode = "link".
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

import httpx

try:
    from .db import get_unenriched, mark_enriched, upsert_asset, DB_PATH
    from .config import load_config
    from .ingest import classify_asset
except ImportError:
    from db import get_unenriched, mark_enriched, upsert_asset, DB_PATH
    from config import load_config
    from ingest import classify_asset

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOGIN_URLS = {
    "unity": "https://id.unity.com/",          # redirects to /en/login
    "fab": "https://www.epicgames.com/id/login",
}
LIBRARY_URLS = {
    # Pages whose network traffic contains owned-asset listings
    "unity": ["https://assetstore.unity.com/purchases",
              "https://assetstore.unity.com/account/purchases"],
    "fab": ["https://www.fab.com/library"],
}

SUCCESS_HINTS = {
    "unity": [r"^https://assetstore\.unity\.com", r"id\.unity\.com/(home|dashboard)"],
    "fab": [r"^https://www\.fab\.com"],
}

# Anti-bot detection: Epic/Unity challenge automation-chromium. Real browsers pass.
LAUNCH_CHANNELS = ["chrome", "msedge", None]     # prefer system Chrome, then Edge
EXTRA_ARGS = ["--disable-blink-features=AutomationControlled"]


def _launch_ctx(p, profile_dir: str, headless: bool):
    """Persistent context preferring a real installed browser (better anti-bot
    behavior than Playwright's bundled chromium). Falls back gracefully."""
    last_err = None
    for channel in LAUNCH_CHANNELS:
        try:
            kwargs = dict(headless=headless, args=EXTRA_ARGS)
            if channel:
                kwargs["channel"] = channel
            return p.chromium.launch_persistent_context(profile_dir, **kwargs)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not launch any browser (chrome/msedge/chromium): {last_err}")


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Interactive login
# ---------------------------------------------------------------------------

def interactive_login(provider: str, timeout_minutes: int = 10) -> bool:
    """Open a visible browser so the user can log in. Saves the profile."""
    pw = _playwright()
    if not pw:
        print("[error] Playwright not installed. Run: pip install playwright && playwright install chromium")
        return False
    cfg = load_config()
    profile_dir = os.path.join(cfg["profiles_dir"], provider)
    os.makedirs(profile_dir, exist_ok=True)

    print(f"\n=== {provider.upper()} LOGIN ===")
    print(f"A browser window will open. Sign in with your {provider} account.")
    print("When the page shows you as logged in, simply CLOSE the browser window.")
    print(f"You have {timeout_minutes} minutes.\n")

    with pw() as p:
        ctx = _launch_ctx(p, profile_dir, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URLS[provider], wait_until="domcontentloaded")

        import time
        deadline = time.time() + timeout_minutes * 60
        ok = False
        while time.time() < deadline:
            try:
                url = page.url or ""
                if any(re.search(pat, url) for pat in SUCCESS_HINTS[provider]):
                    ok = True   # redirected past the login page -> authenticated
                    break
            except Exception:
                ok = True       # browser/window closed by user -> session persisted
                break
            time.sleep(2)

        try:
            ctx.close()
        except Exception:
            pass
    print(f"[{'ok' if ok else 'timeout'}] Login {'saved' if ok else 'not completed'} for {provider}.")
    return ok


def has_saved_session(provider: str) -> bool:
    cfg = load_config()
    return os.path.isdir(os.path.join(cfg["profiles_dir"], provider)) and \
        len(os.listdir(os.path.join(cfg["profiles_dir"], provider))) > 0


# ---------------------------------------------------------------------------
# Library fetch via network interception
# ---------------------------------------------------------------------------

_ASSET_LIST_KEYS = [
    ("results", "name"), ("results", "assetName"), ("items", "title"),
    ("assets", "name"), ("data", "name"), ("hydra:member", "name"),
]


def _looks_like_asset_list(payload: Any) -> List[Dict[str, Any]]:
    """Return a list of asset-like dicts if this JSON payload resembles one."""
    if not isinstance(payload, dict):
        return []
    for list_key, name_key in _ASSET_LIST_KEYS:
        node = payload
        for part in list_key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if isinstance(node, list) and node and isinstance(node[0], dict):
            if name_key in node[0] or "name" in node[0]:
                return node
    return []


def fetch_library(provider: str) -> int:
    """
    Open the store library page using the saved login profile, intercept JSON
    responses, and upsert every discovered asset. Returns count of items seen.
    """
    pw = _playwright()
    if not pw:
        print("[error] Playwright not installed.")
        return 0
    if not has_saved_session(provider):
        print(f"[error] No saved login for '{provider}'. Run interactive_login first.")
        return 0

    cfg = load_config()
    profile_dir = os.path.join(cfg["profiles_dir"], provider)
    harvested: List[Dict[str, Any]] = []
    seen_urls = set()

    def on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            if resp.url in seen_urls:
                return
            body = resp.json()
            items = _looks_like_asset_list(body)
            if items and len(items) >= 3:  # ignore tiny/unrelated payloads
                seen_urls.add(resp.url)
                harvested.extend(items)
                print(f"[harvest] {len(items)} items <- {resp.url[:100]}")
        except Exception:
            pass

    with pw() as p:
        ctx = _launch_ctx(p, profile_dir, headless=cfg["headless_refresh"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", on_response)

        for url in LIBRARY_URLS[provider]:
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                # scroll to trigger lazy loading / pagination
                for _ in range(15):
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(800)
            except Exception as e:
                print(f"[warn] {url}: {e}")
        ctx.close()

    count = 0
    for item in harvested:
        title = item.get("name") or item.get("assetName") or item.get("title")
        if not title:
            continue
        cls = classify_asset(title)
        asset = {
            "id": f"{provider}_{item.get('id') or item.get('listingId') or abs(hash(title))}",
            "source": provider,
            "package_id": str(item.get("id") or item.get("listingId") or ""),
            "title": title,
            "publisher": item.get("sellerName") or item.get("publisher") or item.get("category", ""),
            "version": str(item.get("versionName") or item.get("version") or ""),
            "claimed_date": (item.get("createdAt") or item.get("acquiredAt") or "")[:10],
            "store_url": item.get("url") or item.get("listingUrl") or "",
            "image_url": item.get("image") or item.get("thumbnail") or item.get("keyImage", {}).get("url", "") if isinstance(item.get("keyImage"), dict) else item.get("image") or "",
            **cls,
            "gallery_images": [],
            "video_links": [],
        }
        upsert_asset(asset)
        count += 1

    print(f"[ok] Fetched/updated {count} assets from {provider}.")
    return count


# ---------------------------------------------------------------------------
# Metadata & media enrichment (works without Playwright — plain HTTP)
# ---------------------------------------------------------------------------

_OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_YT_RE = re.compile(r'(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)([\w-]{11})')
_VIDEO_HINTS = re.compile(r'"(https?://[^"]+\.(?:mp4|webm)[^"]*)"', re.I)


def enrich_assets(limit: Optional[int] = None) -> int:
    """
    Fetch store pages for un-enriched assets and extract:
      - og:image (cover art), gallery images
      - YouTube / hosted video links (stored as LINKS only)
      - meta description -> summary fallback
    Returns number of assets enriched.
    """
    cfg = load_config()
    batch = limit or cfg["enrich_batch_size"]
    assets = get_unenriched(batch)
    done = 0

    with httpx.Client(follow_redirects=True, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 (VaultMCP enrichment)"}) as client:
        for asset in assets:
            url = asset.get("store_url") or ""
            if not url.startswith("http"):
                continue
            try:
                r = client.get(url)
                html = r.text
            except Exception as e:
                print(f"[warn] {asset['title']}: {e}")
                continue

            image_url = ""
            m = _OG_IMAGE_RE.search(html)
            if m:
                image_url = m.group(1).replace("&amp;", "&")

            gallery = []
            for g in re.findall(r'https://assetstorev1-prd-cdn\.unity3d\.com/package-screenshot/[^"\' )]+', html)[:8]:
                if g not in gallery:
                    gallery.append(g)

            videos = []
            for vid in _YT_RE.findall(html):
                link = f"https://www.youtube.com/watch?v={vid}"
                if link not in videos:
                    videos.append(link)
                if len(videos) >= 3:
                    break
            for v in _VIDEO_HINTS.findall(html)[:3]:
                if v not in videos:
                    videos.append(v)
            videos = videos[:5]

            desc = ""
            md = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{30,400})["\']', html, re.I)
            if md:
                desc = md.group(1)

            mark_enriched(
                asset["id"],
                image_url=image_url,
                gallery_images=gallery,
                video_links=videos,
                summary=(desc or "")[:400],
            )
            done += 1
            print(f"[enriched] {asset['title']} (img={'Y' if image_url else '-'} vid={len(videos)})")

    print(f"[ok] Enriched {done} assets.")
    return done


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login" and len(sys.argv) > 2:
        interactive_login(sys.argv[2])
    elif cmd == "fetch" and len(sys.argv) > 2:
        fetch_library(sys.argv[2])
    elif cmd == "enrich":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        enrich_assets(n)
    else:
        print("Usage:\n  python -m src.store_client login <unity|fab>\n"
              "  python -m src.store_client fetch <unity|fab>\n"
              "  python -m src.store_client enrich [limit]")
