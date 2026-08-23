"""
VaultMCP store client: interactive login, library fetching, and metadata
enrichment for Unity Asset Store and Epic Games Fab.

Login model (v3 — zero automation at login)
-------------------------------------------
Every previous approach failed because automation touched the login flow:
  - Playwright-bundled chromium: Epic captcha rejects it ("enable javascript").
  - Playwright driving a real browser: injects detectable hooks; Epic throws a
    second "security check" wall after the password.
  - CDP-attached real browser during login: same risk-signal problem.

Final design:
  LOGIN  = the user's own browser opened as a completely plain subprocess
           (no debug port, no attachment, nothing). Signing in is literally
           identical to normal browsing. The user closes the window when
           done; cookies persist in profiles/<provider>-browser/.
  FETCH  = the browser reopens (headed) with the saved session and ONLY THEN
           is a debugger attached to intercept network traffic. Loading
           already-authenticated library pages does not trigger security
           gates; login flows are never automated.

Additional hard rules (learned the hard way):
  - One browser per provider at a time (same-profile relaunch just opens a
    tab in the existing window and breaks everything).
  - Browsers close GRACEFULLY (taskkill without /F); abrupt kills lose
    Unity's device-trust cookie -> MFA on every session.
  - Library fetches never run headless (headless triggers Unity MFA).

Library fetching strategy
-------------------------
Open the store's library page with the saved session, INTERCEPT all JSON
network responses, recursively harvest any asset-like list. Every JSON URL
seen is logged to data/store_harvest.log so failures are diagnosable.
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
    # Login THROUGH the Asset Store's own redirect flow so that
    # assetstore.unity.com receives its OWN session cookie. Logging into
    # id.unity.com alone leaves the store "not logged in" (separate session
    # cookie, severed further by Edge's default third-party cookie blocking).
    "unity": "https://id.unity.com/en/login?redirect_url=https%3A%2F%2Fassetstore.unity.com%2F",
    "fab": "https://www.epicgames.com/id/login",
}

# Our dedicated browser profile needs third-party cookies for the
# assetstore <-> id.unity.com SSO handoff.
_COOKIE_ARGS = [
    "--disable-features=ThirdPartyStoragePartitioning,PartitionedCookies",
]
LIBRARY_URLS = {
    # NOTE: /purchases and /account/purchases are dead (404) as of 2026-08.
    # /account/downloads is the owned-packages page (redirects to login when
    # signed out — verified).
    "unity": ["https://assetstore.unity.com/account/downloads"],
    "fab": ["https://www.fab.com/library"],
}

_LOGIN_MARKERS = ("login", "sign-in", "signin", "id/")

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
            proc.wait(timeout=8)
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


def _launch_browser(cfg, provider: str, start_url: Optional[str] = None,
                    cdp: bool = False) -> tuple:
    """Launch the user's real browser. cdp=False => ZERO automation (login).
    Returns (proc, port) — port is only meaningful when cdp=True."""
    kill_leftover(provider)
    exe = _find_browser()
    if not exe:
        raise RuntimeError("No Chrome/Edge installation found.")
    url = start_url or LOGIN_URLS[provider]
    args = [
        exe,
        f"--user-data-dir={_profile_dir(cfg, provider)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        *_COOKIE_ARGS,
        url,
    ]
    port = 0
    if cdp:
        port = _free_port()
        args.insert(1, f"--remote-debugging-port={port}")
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


# ---------------------------------------------------------------------------
# Interactive login — plain browser, wait for the user to close it
# ---------------------------------------------------------------------------

def interactive_login(provider: str, timeout_minutes: int = 15) -> bool:
    """
    Opens the store sign-in page in a completely NORMAL browser window (no
    automation whatsoever). The user signs in — captcha/MFA behave exactly
    like everyday browsing — then closes the window themselves. Closing is
    the completion signal; cookies persist in the provider profile.
    """
    cfg = load_config()

    # launch with NO debug port: nothing for any risk system to see
    proc, _ = _launch_browser(cfg, provider, cdp=False)
    _log(f"{provider} login: plain browser pid={proc.pid} (no automation)")

    deadline = time.time() + timeout_minutes * 60
    closed_by_user = False
    while time.time() < deadline:
        if proc.poll() is not None:
            closed_by_user = True
            break
        time.sleep(2)

    if not closed_by_user:
        _log(f"{provider} login timed out; closing window.")
        _close_browser_gracefully(proc)

    ok = closed_by_user
    _log(f"{provider} login window {'closed by user' if ok else 'timed out'}. "
         f"If sign-in completed, the session is stored in the profile.")
    return ok


# ---------------------------------------------------------------------------
# Library fetch via network interception (headed, session already established)
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
    """Open the store library page with the saved session (headed), intercept
    JSON responses, upsert discovered assets. Raises visible errors instead of
    failing silently."""
    cfg = load_config()
    if not has_saved_session(provider):
        raise RuntimeError(
            f"No {provider} browser profile found. Press Login first.")

    first_url = LIBRARY_URLS[provider][0]
    # headed + CDP attached on an already-authenticated profile
    proc, port = _launch_browser(cfg, provider, start_url=first_url, cdp=True)
    _log(f"{provider} fetch: browser pid={proc.pid} cdp={port}")

    pw = None
    browser = None
    harvested: List[Dict[str, Any]] = []
    seen_urls = set()
    all_json_urls: List[str] = []
    seen_sigs: set = set()
    landed_on_login = False

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

    def check_urls_for_login_redirect():
        nonlocal landed_on_login
        try:
            urls = [p.url or "" for c in browser.contexts for p in c.pages]
            low = [u.lower() for u in urls]
            if urls and all(any(m in u for m in _LOGIN_MARKERS) or u.startswith(("edge://", "chrome://"))
                            for u in low):
                landed_on_login = True
        except Exception:
            pass

    try:
        pw, browser = _connect_cdp(port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        ctx.on("response", on_response)

        pages = ctx.pages
        page = pages[0] if pages else ctx.new_page()
        try:
            page.bring_to_front()
            page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(5000)

        # scroll to trigger lazy loading / pagination on the first URL
        for _ in range(18):
            check_urls_for_login_redirect()
            if landed_on_login:
                break
            try:
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(700)
            except Exception:
                break

        # remaining library URLs (if any) as extra tabs
        for extra_url in LIBRARY_URLS[provider][1:]:
            if landed_on_login:
                break
            try:
                p2 = ctx.new_page()
                p2.goto(extra_url, wait_until="domcontentloaded", timeout=45000)
                p2.wait_for_timeout(4000)
                for _ in range(10):
                    check_urls_for_login_redirect()
                    if landed_on_login:
                        break
                    p2.mouse.wheel(0, 2500)
                    p2.wait_for_timeout(700)
            except Exception as e:
                _log(f"warn: {extra_url}: {e}")

        check_urls_for_login_redirect()
        time.sleep(2)   # let final cookie writes flush before closing
    finally:
        _close_browser_gracefully(proc)
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            pw.stop()

    if landed_on_login:
        raise RuntimeError(
            f"The {provider} library page redirected to a login screen — "
            f"the saved session is missing or expired. Press Login, sign in, "
            f"close the window, then Fetch again.")

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
        raise RuntimeError(
            f"Loaded the {provider} library but recognized no asset payloads. "
            f"See data/store_harvest.log ({len(all_json_urls)} JSON responses examined).")
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
