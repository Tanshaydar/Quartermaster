"""
VaultMCP store client: interactive login, library fetching, and metadata
enrichment for Unity Asset Store and Epic Games Fab.

Login model
-----------
No public OAuth exists for either store's purchase library, so we drive the
user's REAL browser (Chrome/Edge found on disk) launched as a normal
subprocess with a remote-debugging port, attached over CDP.

Why this design (learned the hard way):
  - Playwright-bundled chromium fails Epic's captcha ("enable javascript").
  - Even Playwright driving a real browser injects detectable hooks.
  - A plain subprocess launch of the user's own browser is indistinguishable
    from manual browsing -> captcha/MFA behave exactly like normal usage.
  - Headless refreshes trigger Unity MFA challenges, so library fetches run
    HEADED (config: headless_refresh=false default).
  - One browser per provider at a time: a second launch with the same profile
    dir merely opens a tab in the existing window and breaks CDP attachment.
  - Browsers must be closed GRACEFULLY (taskkill without /F): an abrupt kill
    loses Unity's device-trust cookie -> MFA demanded on every session.

Session persistence: the browser profile lives in
profiles/<provider>-browser/ and retains cookies between runs.

Library fetching strategy
-------------------------
We open the store's "my assets / library" page and INTERCEPT all JSON network
responses, recursively harvesting any payload that contains an asset-like
list. Every JSON URL seen gets logged to data/store_harvest.log so failures
are diagnosable instead of silent.

Media policy
------------
- Images: cached to disk via server/desktop proxy (media_cache_enabled).
- Videos: links only, never downloaded (video_mode = "link").
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
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
    "unity": "https://id.unity.com/",
    "fab": "https://www.epicgames.com/id/login",
}
LIBRARY_URLS = {
    "unity": ["https://assetstore.unity.com/purchases",
              "https://assetstore.unity.com/account/purchases"],
    "fab": ["https://www.fab.com/library"],
}

SUCCESS_HINTS = {
    "unity": [r"^https://assetstore\.unity\.com", r"id\.unity\.com/(home|dashboard|settings)"],
    "fab": [r"^https://www\.fab\.com", r"epicgames\.com/id/home"],
}


def _is_logged_in(provider: str, urls: List[str]) -> bool:
    """URL-based auth detection, tolerant of unknown post-login landing pages."""
    for u in urls:
        u = (u or "").strip()
        if not u:
            continue
        if any(re.search(pat, u) for pat in SUCCESS_HINTS[provider]):
            return True
        if provider == "unity" and u.startswith("https://id.unity.com/"):
            # left the login page itself -> authenticated (org picker, account, …)
            if "login" not in u and "sign" not in u:
                return True
    return False


# ---------------------------------------------------------------------------
# Browser lifecycle (one instance per provider, graceful shutdown)
# ---------------------------------------------------------------------------

_active_procs: Dict[str, subprocess.Popen] = {}

_BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def _find_browser() -> Optional[str]:
    for c in _BROWSER_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _log(msg: str):
    """Diagnostics go to stdout AND data/store_harvest.log."""
    line = f"[store] {msg}"
    print(line)
    try:
        os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)
        with open(os.path.join(ROOT_DIR, "data", "store_harvest.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _profile_dir(cfg, provider: str) -> str:
    d = os.path.join(cfg["profiles_dir"], f"{provider}-browser")
    os.makedirs(d, exist_ok=True)
    return d


def has_saved_session(provider: str) -> bool:
    cfg = load_config()
    return os.path.isdir(_profile_dir(cfg, provider))


def _close_browser_gracefully(proc: subprocess.Popen):
    """WM_CLOSE first (flushes cookies/session trust), hard-kill as fallback."""
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.wait(timeout=6)
            return
        except Exception:
            pass
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        pass


def kill_leftover(provider: str):
    old = _active_procs.get(provider)
    if old and old.poll() is None:
        _log(f"{provider}: terminating leftover browser (pid {old.pid})")
        _close_browser_gracefully(old)
        time.sleep(1)
    _active_procs[provider] = None


def _launch_browser(cfg, provider: str) -> tuple:
    kill_leftover(provider)
    exe = _find_browser()
    if not exe:
        raise RuntimeError("No Chrome/Edge installation found for store login.")
    port = _free_port()
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_profile_dir(cfg, provider)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-features=msImplicitSignin,msSignIn",
        LOGIN_URLS[provider],
    ]
    proc = subprocess.Popen(args)
    _active_procs[provider] = proc
    return proc, port


def _connect_cdp(port: int, attempts: int = 30):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    last_err = None
    for _ in range(attempts):
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            return pw, browser
        except Exception as e:
            last_err = e
            time.sleep(1)
    pw.stop()
    raise RuntimeError(f"Could not attach to browser CDP: {last_err}")


def _single_fresh_tab(ctx, provider: str, target_url: Optional[str] = None,
                      settle_ms: int = 3000):
    """Close ALL pages (stale tabs poison detection & launches), open one page
    on target_url (defaults to the provider login page)."""
    url_to_open = target_url or LOGIN_URLS[provider]
    for pg in list(ctx.pages):
        try:
            pg.close()
        except Exception:
            pass
    page = ctx.new_page()
    try:
        page.goto(url_to_open, wait_until="domcontentloaded", timeout=45000)
        if settle_ms:
            page.wait_for_timeout(settle_ms)
    except Exception as e:
        _log(f"warn: navigation to {url_to_open}: {e}")
    return page


# ---------------------------------------------------------------------------
# Interactive login
# ---------------------------------------------------------------------------

def interactive_login(provider: str, timeout_minutes: int = 10) -> bool:
    """Open the user's real browser for manual sign-in (MFA/captcha all work).
    Session persists in the provider's browser profile for later fetches.
    Returns True when authenticated; the window closes itself on success."""
    cfg = load_config()

    proc, port = _launch_browser(cfg, provider)
    _log(f"{provider} login: browser pid={proc.pid} cdp={port}")

    pw = None
    browser = None
    ok = False
    try:
        pw, browser = _connect_cdp(port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        _single_fresh_tab(ctx, provider)

        deadline = time.time() + timeout_minutes * 60
        while time.time() < deadline:
            if proc.poll() is not None:
                ok = True   # user closed the browser -> session persisted
                break
            try:
                urls = [p.url for ctx2 in browser.contexts for p in ctx2.pages]
                if _is_logged_in(provider, urls):
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(2)

        if ok and proc.poll() is None:
            time.sleep(2)   # let cookies flush before closing
            _log(f"{provider}: login detected, closing browser.")
    finally:
        _close_browser_gracefully(proc)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            pw.stop()

    _log(f"{provider} login {'saved' if ok else 'timed out/not completed'}.")
    return ok


# ---------------------------------------------------------------------------
# Library fetch via network interception (headed — headless triggers MFA)
# ---------------------------------------------------------------------------

_NAME_KEYS = ("name", "assetName", "title", "asset_name", "productName")


def _walk_for_asset_lists(node: Any, out: List[list], depth: int = 0):
    if depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, list) and len(v) >= 3 and \
                    all(isinstance(x, dict) for x in v[:3]) and \
                    any(nk in v[0] for nk in _NAME_KEYS):
                out.append(v)
            else:
                _walk_for_asset_lists(v, out, depth + 1)
    elif isinstance(node, list):
        for x in node[:20]:
            _walk_for_asset_lists(x, out, depth + 1)


def _looks_like_asset_lists(payload: Any) -> List[list]:
    out: List[list] = []
    try:
        _walk_for_asset_lists(payload, out)
    except Exception:
        pass
    return out


def fetch_library(provider: str) -> int:
    """Open the store library page in the user's real browser (headed), intercept
    JSON responses, and upsert every discovered asset. Returns items seen."""
    cfg = load_config()
    if not has_saved_session(provider):
        raise RuntimeError(f"No saved {provider} session. Run Login first.")

    proc, port = _launch_browser(cfg, provider)
    _log(f"{provider} fetch: browser pid={proc.pid} cdp={port}")

    pw = None
    browser = None
    harvested: List[Dict[str, Any]] = []
    seen_urls = set()
    all_json_urls: List[str] = []
    seen_sigs: set = set()

    def on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            url = resp.url
            if url not in seen_urls:
                seen_urls.add(url)
                all_json_urls.append(url)
            body = resp.json()
            for lst in _looks_like_asset_lists(body):
                sig = f"{url}|{len(lst)}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                _log(f"  candidate list @ {url[:110]} "
                     f"({len(lst)} items, keys={sorted(list(lst[0].keys()))[:8]})")
                harvested.extend(lst)
        except Exception:
            pass

    try:
        pw, browser = _connect_cdp(port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()

        for url in LIBRARY_URLS[provider]:
            page = _single_fresh_tab(ctx, provider, target_url=url, settle_ms=4000)
            for _ in range(15):
                try:
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(700)
                except Exception:
                    break
            try:
                page.close()
            except Exception:
                pass
    finally:
        _close_browser_gracefully(proc)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            pw.stop()

    count = 0
    unknown = 0
    for item in harvested:
        title = None
        for k in _NAME_KEYS:
            if item.get(k):
                title = str(item[k]).strip()
                break
        if not title:
            unknown += 1
            continue
        cls = classify_asset(title)
        image = item.get("image") or item.get("thumbnail") or ""
        if isinstance(item.get("keyImage"), dict):
            image = item["keyImage"].get("url") or image
        asset = {
            "id": f"{provider}_{item.get('id') or item.get('listingId') or abs(hash(title))}",
            "source": provider,
            "package_id": str(item.get("id") or item.get("listingId") or ""),
            "title": title,
            "publisher": str(item.get("sellerName") or item.get("publisher") or "")[:120],
            "version": str(item.get("versionName") or item.get("version") or ""),
            "claimed_date": (item.get("createdAt") or item.get("acquiredAt") or "")[:10],
            "store_url": item.get("url") or item.get("listingUrl") or "",
            "image_url": image if isinstance(image, str) else "",
            **cls,
            "gallery_images": [],
            "video_links": [],
        }
        upsert_asset(asset)
        count += 1

    _log(f"{provider} fetch done: {count} assets upserted "
         f"({unknown} entries had no usable title).")

    if count == 0:
        _log(f"HARVEST DIAGNOSTIC for {provider}: {len(all_json_urls)} JSON responses seen:")
        for u in all_json_urls[:40]:
            _log(f"  json: {u[:150]}")

    if count == 0 and unknown == 0 and len(all_json_urls) > 0:
        raise RuntimeError(
            f"Opened {provider} library but recognized no asset payloads. "
            f"See data/store_harvest.log ({len(all_json_urls)} JSON responses were examined).")
    return count


# ---------------------------------------------------------------------------
# Metadata & media enrichment (plain HTTP, no browser needed)
# ---------------------------------------------------------------------------

_OG_IMAGE_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_YT_RE = re.compile(r'(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)([\w-]{11})')
_VIDEO_HINTS = re.compile(r'"(https?://[^"]+\.(?:mp4|webm)[^"]*)"', re.I)


def enrich_assets(limit: Optional[int] = None) -> int:
    """
    Fetch store pages for un-enriched assets and extract cover art, gallery
    images, video LINKS, and description. Returns number enriched.
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
                _log(f"warn: {asset['title']}: {e}")
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
            _log(f"enriched: {asset['title']} (img={'Y' if image_url else '-'} vid={len(videos)})")

    _log(f"enrichment done: {done} assets.")
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
