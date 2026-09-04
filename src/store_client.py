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

import hashlib
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
    from .db import (get_unenriched, mark_enriched, upsert_asset, DB_PATH,
                     get_connection)
    from .config import load_config, HARVEST_LOG_PATH
    from .ingest import classify_asset, _stable_id
except ImportError:
    from db import (get_unenriched, mark_enriched, upsert_asset, DB_PATH,
                    get_connection)
    from config import load_config, HARVEST_LOG_PATH
    from ingest import classify_asset, _stable_id

LOGIN_URLS = {
    # Login THROUGH the Asset Store's own redirect flow so that
    # assetstore.unity.com receives its OWN session cookie. Logging into
    # id.unity.com alone leaves the store "not logged in" (separate session
    # cookie, severed further by Edge's default third-party cookie blocking).
    "unity": "https://id.unity.com/en/login?redirect_url=https%3A%2F%2Fassetstore.unity.com%2F",
    "fab": "https://www.epicgames.com/id/login",
    "gumroad": "https://gumroad.com/login",
    "cosmos": "https://cosmos.leartesstudios.com/signin",
}

LIBRARY_URLS = {
    # NOTE: /purchases and /account/purchases are dead (404) as of 2026-08.
    # /account/downloads redirects here; this is the owned-packages page.
    "unity": ["https://assetstore.unity.com/account/assets"],
    "fab": ["https://www.fab.com/library"],
    "gumroad": ["https://gumroad.com/library"],
    "cosmos": ["https://cosmos.leartesstudios.com/inventory"],
}

_LOGIN_MARKERS = ("login", "sign-in", "signin", "id/")

# Restrict passive network harvesting to the store's own origins. Without
# this, the generic list detector happily mistakes Edge newsfeed cards,
# Google autocomplete payloads, etc. for asset listings.
_PROVIDER_HOSTS = {
    "unity": ("unity.com", "unity3d.com"),
    "fab": ("fab.com", "epicgames.com"),
    "gumroad": ("gumroad.com", "gumroadcdn.com"),
    "cosmos": ("leartesstudios.com",),
}


def _url_host_in_provider(url: str, provider: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d)
               for d in _PROVIDER_HOSTS.get(provider, ()))

# ---------------------------------------------------------------------------
# Browser lifecycle (one instance per provider, graceful shutdown)
# ---------------------------------------------------------------------------

_active_procs: Dict[str, subprocess.Popen] = {}

_BROWSER_CANDIDATES = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # Linux & BSD
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/microsoft-edge",
    "/snap/bin/chromium",
    "/var/lib/flatpak/exports/bin/com.google.Chrome",
    "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

_PATH_BINARIES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "chrome",
    "msedge",
]


def _find_browser() -> Optional[str]:
    import shutil
    # 1. Check PATH dynamically
    for b in _PATH_BINARIES:
        found = shutil.which(b)
        if found and os.path.isfile(found):
            return found
    # 2. Check well-known filesystem locations
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
        from .config import rotate_log_if_large
        log_file = HARVEST_LOG_PATH
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        rotate_log_if_large(log_file)
        with open(log_file, "a", encoding="utf-8") as f:
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


def _is_pid_alive(pid: int) -> bool:
    """Check if a given process ID is currently running on the system."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _acquire_profile_lock(prof_dir: str, provider: str) -> str:
    """Ensure no other process is actively running a browser session on this profile."""
    lock_file = os.path.join(prof_dir, ".profile_lock")
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            if _is_pid_alive(old_pid):
                active_child = _active_procs.get(provider)
                if not active_child or active_child.pid != old_pid:
                    raise RuntimeError(
                        f"A browser session for {provider} is already running in another process (PID {old_pid}). "
                        f"Please close that browser window before starting a new session.")
        except ValueError:
            pass
    return lock_file


def _release_profile_lock(prof_dir: str):
    lock_file = os.path.join(prof_dir, ".profile_lock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass


def _close_browser_gracefully(proc: subprocess.Popen, prof_dir: Optional[str] = None):
    """WM_CLOSE first (flushes cookies/session trust), hard-kill as fallback."""
    if prof_dir:
        _release_profile_lock(prof_dir)
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
    cfg = load_config()
    prof_dir = _profile_dir(cfg, provider)
    if old and old.poll() is None:
        _log(f"{provider}: terminating leftover browser (pid {old.pid})")
        _close_browser_gracefully(old, prof_dir)
        time.sleep(1)
    else:
        _release_profile_lock(prof_dir)
    _active_procs[provider] = None


def _launch_browser(cfg, provider: str, start_url: Optional[str] = None,
                    cdp: bool = False) -> tuple:
    """Launch the user's real browser. cdp=False => ZERO automation (login).
    Returns (proc, port) — port is only meaningful when cdp=True."""
    kill_leftover(provider)
    prof_dir = _profile_dir(cfg, provider)
    lock_file = _acquire_profile_lock(prof_dir, provider)
    exe = _find_browser()
    if not exe:
        raise RuntimeError("No Chrome/Edge installation found.")
    url = start_url or LOGIN_URLS[provider]
    args = [
        exe,
        f"--user-data-dir={prof_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        # cookie partitioning off (SSO handoff); background mode off (closing
        # the last window must terminate the process, or login-wait hangs)
        "--disable-features=ThirdPartyStoragePartitioning,PartitionedCookies,"
        "BackgroundMode,msEdgeBackgroundMode",
        url,
    ]
    port = 0
    if cdp:
        port = _free_port()
        args.insert(1, f"--remote-debugging-port={port}")
    proc = subprocess.Popen(args)
    _active_procs[provider] = proc
    try:
        with open(lock_file, "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
    except Exception:
        pass
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

def interactive_login(provider: str, timeout_minutes: int = 15,
                      cancel_event=None) -> bool:
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
        if cancel_event is not None and cancel_event.is_set():
            _log(f"{provider} login cancelled.")
            break
        if proc.poll() is not None:
            closed_by_user = True
            break
        time.sleep(2)

    if not closed_by_user:
        reason = "cancelled" if (cancel_event and cancel_event.is_set()) else "timed out"
        _log(f"{provider} login {reason}; closing window.")
        _close_browser_gracefully(proc, _profile_dir(cfg, provider))
    else:
        _release_profile_lock(_profile_dir(cfg, provider))

    ok = closed_by_user
    _log(f"{provider} login window {'closed by user' if ok else 'timed out'}. "
         f"If sign-in completed, the session is stored in the profile.")
    return ok


# ---------------------------------------------------------------------------
# Library fetch via network interception (headed, session already established)
# ---------------------------------------------------------------------------

_NAME_KEYS = ("name", "assetName", "title", "asset_name", "productName", "displayName")

def _extract_string(val: Any, *subkeys: str) -> str:
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for sk in subkeys or ("name", "label", "title", "url", "value", "href", "slug"):
            if val.get(sk) and isinstance(val[sk], str):
                return val[sk].strip()
    return ""


def _extract_store_url(item: dict, provider: str) -> str:
    for k in ("url", "listingUrl", "packageUrl", "link", "webUrl", "canonicalUrl"):
        u = _extract_string(item.get(k), "url", "href", "slug")
        if u:
            if u.startswith("http"):
                return u
            if u.startswith("/"):
                if provider == "unity":
                    return f"https://assetstore.unity.com{u}"
                elif provider == "fab":
                    return f"https://www.fab.com{u}"
                elif provider == "gumroad":
                    return f"https://gumroad.com{u}"
                elif provider == "cosmos":
                    return f"https://cosmos.leartesstudios.com{u}"
                return f"https://www.fab.com{u}"
            return u
    slug = item.get("slug") or item.get("packageSlug") or item.get("listingSlug") or item.get("permalink")
    if slug and isinstance(slug, str):
        if provider == "unity":
            return f"https://assetstore.unity.com/packages/{slug.strip('/')}"
        elif provider == "fab":
            return f"https://www.fab.com/listings/{slug.strip('/')}"
        elif provider == "gumroad":
            return f"https://gumroad.com/library?item={slug.strip('/')}"
        elif provider == "cosmos":
            return f"https://cosmos.leartesstudios.com/product/{slug.strip('/')}"
    pkg_id = item.get("id") or item.get("listingId") or item.get("packageId")
    if pkg_id:
        if provider == "unity" and str(pkg_id).isdigit():
            return f"https://assetstore.unity.com/packages/slug/{pkg_id}"
        elif provider == "fab":
            return f"https://www.fab.com/listings/{pkg_id}"
        elif provider == "gumroad":
            # If 32-char hex token, it's a direct download/library page
            clean_id = str(pkg_id).strip()
            if len(clean_id) == 32 and all(c in "0123456789abcdefABCDEF" for c in clean_id):
                return f"https://gumroad.com/d/{clean_id}"
            return f"https://gumroad.com/library?item={clean_id}"
        elif provider == "cosmos":
            return f"https://cosmos.leartesstudios.com/product/{pkg_id}"
    return ""


def _extract_media_images(item: dict) -> tuple:
    """Extract primary cover art and gallery screenshots from any store payload format."""
    cover = ""
    gallery = []

    # 1. Fab's thumbnails structure (thumbnails: [{mediaUrl: "...", images: [{url: "...", width: ...}]}])
    th_list = item.get("thumbnails")
    if isinstance(th_list, list):
        for th in th_list:
            if isinstance(th, dict):
                best_thumb = ""
                mu = th.get("mediaUrl")
                if isinstance(mu, str) and mu.startswith("http"):
                    best_thumb = mu
                else:
                    sub_imgs = th.get("images")
                    if isinstance(sub_imgs, list) and sub_imgs:
                        sorted_sub = sorted([x for x in sub_imgs if isinstance(x, dict) and x.get("url")],
                                            key=lambda x: x.get("width", 0), reverse=True)
                        if sorted_sub:
                            best_thumb = sorted_sub[0]["url"]
                if best_thumb:
                    if not cover:
                        cover = best_thumb
                    if best_thumb not in gallery:
                        gallery.append(best_thumb)

    # 2. Direct string fields
    for k in ("cover_image", "coverImage", "coverUrl", "cover_url", "heroUrl", "previewUrl", "preview_url", "mainImage", "primaryImage", "image", "imageUrl", "thumbnailUrl", "thumbnail", "thumbnail_url", "mediaUrl"):
        v = item.get(k)
        if isinstance(v, str) and v.startswith("http"):
            if not cover:
                cover = v
            if v not in gallery:
                gallery.append(v)
            break
        if isinstance(v, dict):
            for sub in ("url", "href", "src"):
                if isinstance(v.get(sub), str) and v[sub].startswith("http"):
                    if not cover:
                        cover = v[sub]
                    if v[sub] not in gallery:
                        gallery.append(v[sub])
                    break
        if cover:
            break

    # 3. Key Image dict
    if not cover:
        for k in ("keyImage", "mainImage", "primaryImage", "cover"):
            ki = item.get(k)
            if isinstance(ki, dict):
                for sub in ("url", "href", "src"):
                    if isinstance(ki.get(sub), str) and ki[sub].startswith("http"):
                        cover = ki[sub]
                        if cover not in gallery:
                            gallery.append(cover)
                        break
            if cover:
                break

    # 4. Media / Screenshots array
    for arr_key in ("media", "images", "gallery", "screenshots"):
        arr = item.get(arr_key)
        if isinstance(arr, list):
            for elem in arr:
                u = ""
                if isinstance(elem, str) and elem.startswith("http"):
                    u = elem
                elif isinstance(elem, dict):
                    for sub in ("url", "src", "href", "imageUrl", "mediaUrl"):
                        if isinstance(elem.get(sub), str) and elem[sub].startswith("http"):
                            u = elem[sub]
                            break
                if u and u not in gallery:
                    gallery.append(u)
                if not cover and u:
                    cover = u

    return cover, gallery[:8]


def _extract_publisher(item: dict) -> str:
    for k in ("sellerName", "publisher", "publisherLabel", "publisherName", "developer", "author", "creator", "user", "studio"):
        p = _extract_string(item.get(k), "name", "label", "sellerName", "username", "displayName")
        if p:
            return p[:120]
    return ""



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


_SCROLL_JS = """
() => {
    // find the largest scrollable element (SPA pages often use an inner
    // overflow container; window.scrollTo does nothing there)
    let best = null;
    for (const el of document.querySelectorAll('div, main, section')) {
        if (el.scrollHeight > el.clientHeight + 100) {
            const oy = getComputedStyle(el).overflowY;
            if ((oy === 'auto' || oy === 'scroll') &&
                (!best || el.scrollHeight > best.scrollHeight)) best = el;
        }
    }
    if (best) {
        best.scrollTop = best.scrollHeight;
        return 'container:' + (best.className || best.id || 'div').toString().slice(0, 40);
    }
    window.scrollTo(0, document.documentElement.scrollHeight);
    return 'window';
}
"""


# ---------------------------------------------------------------------------
# Fab native harvest (/i/library/search — plain REST, cursor-paginated)
#
# Diagnostics from data/store_harvest.log showed Fab's authenticated library
# feed is a simple REST endpoint (NOT GraphQL like Unity): each response
# carries a page of acquired listings plus an opaque cursor for the next.
# Replaying it from the page context gives authoritative completeness —
# the property Unity gets from myAssets.
# ---------------------------------------------------------------------------

_FAB_LIBRARY_API = "https://www.fab.com/i/library/search"

_FAB_FETCH_JS = """
async (args) => {
    const r = await fetch(args.url, {
        credentials: 'include',
        headers: {'Accept': 'application/json'}
    });
    if (!r.ok) return {"__http_status": r.status};
    return await r.json();
}
"""


def _fab_find_results(node, depth=0):
    """Locate the listing array in a /i/library/search payload (schema-tolerant)."""
    if depth > 4 or not isinstance(node, dict):
        return None
    for key in ("results", "elements", "content", "items", "listings", "data"):
        v = node.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    for v in node.values():
        if isinstance(v, dict):
            found = _fab_find_results(v, depth + 1)
            if found is not None:
                return found
    return None


def _fab_find_cursor(node, depth=0):
    """Find the next-page cursor wherever the API hides it."""
    if depth > 5 or not isinstance(node, dict):
        return ""
    for k, v in node.items():
        if "cursor" in k.lower() and isinstance(v, str) and v:
            return v
        found = _fab_find_cursor(v, depth + 1)
        if found:
            return found
    return ""


def _fab_flatten(item: Dict[str, Any]) -> Dict[str, Any]:
    """Fab wraps listing, seller, and media in sub-objects; merge all inner
    dictionaries so thumbnails, screenshots, key images, and descriptions
    are never dropped."""
    if not isinstance(item, dict):
        return {}
    out = dict(item)
    for k, v in item.items():
        if isinstance(v, dict):
            for subk, subv in v.items():
                if subk not in out or not out[subk]:
                    out[subk] = subv
    return out


def harvest_fab_library(page, known_titles: Optional[set] = None) -> List[Dict[str, Any]]:
    """Replay Fab's own library-search endpoint with pagination until exhausted.
    Real payload shape (verified live):
      {"aggregations": …, "cursors": {…}, "next": <full URL|null>,
       "previous": …, "results": [listing, …]} — follow top-level "next"."""
    from urllib.parse import quote
    if known_titles is None:
        known_titles = set()
    collected: List[Dict[str, Any]] = []
    url = f"{_FAB_LIBRARY_API}?sort_by=-createdAt&source=acquired&count=50"
    pages = 0
    while pages < 200:          # safety cap; real vaults paginate well below this
        body = page.evaluate(_FAB_FETCH_JS, {"url": url})
        if not isinstance(body, dict) or "__http_status" in body:
            status = body.get("__http_status") if isinstance(body, dict) else type(body).__name__
            _log(f"  fab harvest: bad response ({status}); stopping.")
            break
        items = _fab_find_results(body)
        if items is None:
            _log(f"  [fab diag] unrecognized payload shape: top-keys={list(body.keys())[:8]}")
            break

        # One-time schema capture: what does a raw listing item actually carry?
        # (media depth, slugs for building listing URLs, video fields, …)
        _sample_path = os.path.join(ROOT_DIR, "data", "fab_library_sample.json")
        if not os.path.exists(_sample_path):
            try:
                probe = {
                    "top_keys": list(body.keys()),
                    "first_item": items[0],
                    "item_count_this_page": len(items),
                }
                with open(_sample_path, "w", encoding="utf-8") as _sf:
                    _sf.write(json.dumps(probe, ensure_ascii=False)[:100_000])
                _log("  [diag] saved first fab item -> data/fab_library_sample.json")
            except Exception:
                pass
        if pages == 0 and items:
            sample_flat = _fab_flatten(items[0])
            _log(f"  [fab sample] keys={list(sample_flat.keys())[:12]}")
            cover_sample, gallery_sample = _extract_media_images(sample_flat)
            _log(f"  [fab sample] cover={cover_sample[:60] if cover_sample else 'none'} | gallery={len(gallery_sample)}")
        new = 0
        for raw in items:
            item = _fab_flatten(raw)
            title = str(item.get("name") or item.get("title") or "").strip()
            if not title or title in known_titles:
                continue
            known_titles.add(title)
            # The top-level library-entry uid is the canonical listing anchor:
            # /library/assets/<this uid> is what Fab itself declares as og:url.
            if raw.get("uid"):
                item["_lib_uid"] = str(raw["uid"])
            collected.append(item)
            new += 1
        pages += 1
        _log(f"  fab harvest: page {pages} (+{new} this page, {len(collected)} total)")

        # Follow Fab's documented pagination: top-level "next" carries the
        # full next-page URL (null on the last page).
        nxt = body.get("next")
        if isinstance(nxt, str) and nxt.startswith("http"):
            url = nxt
            continue
        # Defensive fallbacks in case the API shape shifts again.
        tok = ""
        cur = body.get("cursors")
        if isinstance(cur, dict) and isinstance(cur.get("next"), str) and cur["next"]:
            tok = cur["next"]
        if not tok:
            tok = _fab_find_cursor(body)
        if tok:
            url = (f"{_FAB_LIBRARY_API}?sort_by=-createdAt&source=acquired"
                   f"&count=50&cursor={quote(tok, safe='')}")
            continue
        _log(f"  fab harvest complete: {len(collected)} listings across {pages} pages")
        break
    if not collected and pages == 0:
        _log("  fab harvest produced nothing — see [fab diag] above for payload shape")
    return collected


_GUMROAD_FETCH_JS = """
(() => {
    // 1. Inertia.js (Gumroad's modern architecture)
    try {
        const pageEl = document.querySelector('[data-page]');
        if (pageEl) {
            const initial = JSON.parse(pageEl.getAttribute('data-page') || '{}');
            const props = initial.props || {};
            const results = props.results || [];
            const totalPages = props.pagination?.pages || 1;

            const allItems = [];
            function extractItems(resList) {
                if (!Array.isArray(resList)) return;
                for (const r of resList) {
                    const prod = r.product || {};
                    const pur = r.purchase || {};
                    const dlUrl = pur.download_url || '';
                    let uid = '';
                    if (dlUrl) {
                        const m = dlUrl.match(/\\/d\\/([a-zA-Z0-9_-]+)/);
                        if (m) uid = m[1];
                    }
                    if (!uid && pur.id) uid = pur.id;
                    if (!uid && prod.name) uid = prod.name.toLowerCase().replace(/[^a-z0-9]+/g, '-');

                    const name = prod.name || '';
                    if (!name) continue;

                    const creatorName = prod.creator?.name || r.creator_name || '';
                    const cover = prod.thumbnail_url || prod.cover_image || '';
                    const storeUrl = dlUrl || (uid ? ('https://gumroad.com/d/' + uid) : '');

                    let desc = '';
                    if (pur.variants) {
                        desc = 'License / Variant: ' + pur.variants;
                    }

                    allItems.push({
                        id: uid,
                        name: name.trim(),
                        publisher: creatorName.trim(),
                        description: desc,
                        cover_image: cover,
                        url: storeUrl,
                        variants: pur.variants || '',
                    });
                }
            }

            extractItems(results);

            // Return a promise to fetch and parse subsequent pages via DOMParser
            return (async () => {
                for (let pageNum = 2; pageNum <= totalPages; pageNum++) {
                    try {
                        const res = await fetch('/library?page=' + pageNum);
                        const html = await res.text();
                        const doc = new DOMParser().parseFromString(html, 'text/html');
                        const nextEl = doc.querySelector('[data-page]');
                        if (nextEl) {
                            const nextData = JSON.parse(nextEl.getAttribute('data-page') || '{}');
                            extractItems(nextData.props?.results || []);
                        }
                    } catch (err) {
                        console.error('Gumroad fetch page ' + pageNum + ' failed:', err);
                    }
                }
                return allItems;
            })();
        }
    } catch (e) {
        console.error('Gumroad Inertia extraction error:', e);
    }

    // 2. Next.js state fallback
    try {
        const nextEl = document.getElementById('__NEXT_DATA__');
        if (nextEl) {
            const nextData = JSON.parse(nextEl.textContent || '{}');
            const purchases = nextData.props?.pageProps?.purchases 
                || nextData.props?.pageProps?.initialState?.purchases
                || nextData.props?.pageProps?.data?.purchases || [];
            if (Array.isArray(purchases) && purchases.length > 0) {
                return purchases.map(p => ({
                    id: String(p.id || p.permalink || p.product_id || ''),
                    name: String(p.product?.name || p.name || p.product_name || ''),
                    publisher: String(p.creator?.name || p.seller?.name || p.creator_name || p.product?.creator?.name || ''),
                    description: String(p.product?.description || p.description || ''),
                    cover_image: String(p.product?.preview_url || p.product?.thumbnail_url || p.thumbnail_url || p.preview_url || ''),
                    url: p.permalink ? ('https://gumroad.com/library?item=' + p.permalink) : String(p.url || ''),
                    createdAt: String(p.created_at || p.purchase_date || ''),
                    tags: p.product?.tags || [],
                }));
            }
        }
    } catch (e) {}

    // 3. Fallback: DOM scraping
    const items = [];
    try {
        const cards = document.querySelectorAll('article, .library-item, [data-component="LibraryItem"], .product-card, a[href*="/library?item="]');
        for (const card of cards) {
            const titleEl = card.querySelector('h3, h4, .product-title, .title, [class*="title"], [class*="Title"]') || card;
            const title = (titleEl.textContent || '').trim();
            if (!title || title.length > 180) continue;

            const linkEl = card.tagName === 'A' ? card : card.querySelector('a[href*="/library"], a[href*="/d/"], a[href*="/l/"], a');
            const href = linkEl ? linkEl.getAttribute('href') : '';
            const fullUrl = href ? (href.startsWith('http') ? href : ('https://gumroad.com' + href)) : '';

            const authorEl = card.querySelector('.seller-name, .creator-name, [class*="seller"], [class*="creator"], [class*="author"]');
            const author = authorEl ? authorEl.textContent.trim() : '';

            const imgEl = card.querySelector('img');
            const cover = imgEl ? (imgEl.getAttribute('src') || '') : '';

            const descEl = card.querySelector('p, .description, [class*="description"]');
            const desc = descEl ? descEl.textContent.trim() : '';

            let uid = card.getAttribute('data-product-id') || card.getAttribute('data-id') || '';
            if (!uid && href) {
                const match = href.match(/[?&]item=([^&]+)/) || href.match(/\\/l\\/([^/?#]+)/) || href.match(/\\/d\\/([^/?#]+)/);
                if (match) uid = match[1];
            }
            if (!uid) uid = title.toLowerCase().replace(/[^a-z0-9]+/g, '-');

            items.push({
                id: uid,
                name: title,
                publisher: author,
                description: desc,
                cover_image: cover,
                url: fullUrl,
            });
        }
    } catch (e) {}
    return items;
})()
"""

_COSMOS_FETCH_JS = """
(async () => {
    // 1. Direct authenticated API pagination using Bearer token from localStorage
    try {
        let token = localStorage.getItem('token');
        if (token && token.startsWith('"') && token.endsWith('"')) {
            try { token = JSON.parse(token); } catch (e) {}
        }
        const headers = { 'Accept': 'application/json' };
        if (token) {
            headers['Authorization'] = 'Bearer ' + token;
        }
        const firstRes = await fetch('https://api.cosmos.leartesstudios.com/inventory?page=1', { headers, credentials: 'include' });
        if (firstRes.ok) {
            const firstJson = await firstRes.json();
            const lastPage = firstJson.meta?.last_page || 1;
            const rawList = [...(firstJson.data || [])];

            if (lastPage > 1) {
                const promises = [];
                for (let p = 2; p <= lastPage; p++) {
                    promises.push(
                        fetch('https://api.cosmos.leartesstudios.com/inventory?page=' + p, { headers, credentials: 'include' })
                            .then(r => r.ok ? r.json() : {})
                            .then(j => j.data || [])
                            .catch(() => [])
                    );
                }
                const pages = await Promise.all(promises);
                for (const pg of pages) {
                    rawList.push(...pg);
                }
            }

            if (rawList.length > 0) {
                return rawList.map(item => {
                    const coverUrl = (typeof item.cover_image === 'object' && item.cover_image?.url)
                        ? item.cover_image.url
                        : (typeof item.cover_image === 'string' ? item.cover_image : '');
                    const slug = item.slug || item.id || '';
                    const storeUrl = slug ? ('https://cosmos.leartesstudios.com/product/' + slug) : '';
                    let license = item.license || 'individual';
                    let desc = item.subtitle || item.description || '';
                    if (license) {
                        desc = (desc ? (desc + ' · ') : '') + 'License: ' + license;
                    }
                    return {
                        id: String(item.id || slug),
                        slug: slug,
                        title: String(item.title || item.name || ''),
                        name: String(item.title || item.name || ''),
                        publisher: 'Leartes Studios',
                        description: desc,
                        cover_image: coverUrl,
                        url: storeUrl,
                        category: String(item.type || 'Environments'),
                        type: String(item.type || ''),
                        license: String(license),
                    };
                });
            }
        }
    } catch (err) {
        console.error('Cosmos API harvest error:', err);
    }

    // 2. Next.js hydration payload fallback
    try {
        const nextEl = document.getElementById('__NEXT_DATA__');
        if (nextEl) {
            const nextData = JSON.parse(nextEl.textContent || '{}');
            const pp = nextData.props?.pageProps || {};
            const assets = pp.inventory
                || pp.assets 
                || pp.library 
                || pp.myAssets
                || pp.items
                || pp.data?.inventory
                || pp.data?.items
                || pp.data?.assets
                || pp.data || [];
            if (Array.isArray(assets) && assets.length > 0) {
                return assets.map(a => {
                    const item = a.product || a.asset || a;
                    const coverUrl = (typeof item.cover_image === 'object' && item.cover_image?.url)
                        ? item.cover_image.url
                        : (item.cover_image || item.coverImage || item.thumbnail || item.image || item.cover_url || item.preview_url || '');
                    const slug = item.slug || item.id || '';
                    return {
                        id: String(item.id || slug || a.id || ''),
                        slug: String(slug),
                        name: String(item.title || item.name || a.name || ''),
                        title: String(item.title || item.name || a.name || ''),
                        publisher: String(item.creator || item.publisher || item.seller || 'Leartes Studios'),
                        description: String(item.subtitle || item.description || ''),
                        cover_image: String(coverUrl),
                        gallery_images: item.gallery || item.images || [],
                        url: slug ? ('https://cosmos.leartesstudios.com/product/' + slug) : String(item.url || ''),
                        category: typeof item.category === 'object' ? item.category?.name : (item.type || item.category || 'Environments'),
                        technicalSpecs: typeof item.specs === 'object' ? JSON.stringify(item.specs) : (item.specs || ''),
                    };
                });
            }
        }
    } catch (e) {}

    // 3. DOM scraping fallback
    const items = [];
    try {
        const cards = document.querySelectorAll('.asset-card, [class*="AssetCard"], [class*="product-card"], [class*="assetCard"], .card, [class*="inventory"], [class*="Inventory"]');
        for (const card of cards) {
            const titleEl = card.querySelector('h3, h4, h5, [class*="title"], [class*="Title"], .name, [class*="name"]');
            if (!titleEl) continue;
            const title = (titleEl.textContent || '').trim();
            if (!title || title.length > 180) continue;

            const linkEl = card.querySelector('a[href*="/product"], a[href*="/asset"], a[href*="/inventory"], a');
            const href = linkEl ? linkEl.getAttribute('href') : '';
            const fullUrl = href ? (href.startsWith('http') ? href : ('https://cosmos.leartesstudios.com' + href)) : '';

            const imgEl = card.querySelector('img');
            const cover = imgEl ? (imgEl.getAttribute('src') || '') : '';

            const catEl = card.querySelector('[class*="category"], [class*="badge"], [class*="tag"]');
            const cat = catEl ? catEl.textContent.trim() : '';

            let uid = '';
            if (href) {
                const match = href.match(/\\/(?:product|asset|inventory)s?\\/([^/?#]+)/);
                if (match) uid = match[1];
            }
            if (!uid) uid = title.toLowerCase().replace(/[^a-z0-9]+/g, '-');

            items.push({
                id: uid,
                name: title,
                publisher: 'Leartes Studios',
                cover_image: cover,
                url: fullUrl,
                category: cat || 'Environments',
            });
        }
    } catch (e) {}
    return items;
})()
"""

def harvest_gumroad_library(page) -> List[Dict[str, Any]]:
    """Extract purchased assets from Gumroad library via state inspection or DOM cards."""
    try:
        raw = page.evaluate(_GUMROAD_FETCH_JS)
        if isinstance(raw, list):
            _log(f"  gumroad harvest extracted {len(raw)} items")
            return [x for x in raw if isinstance(x, dict) and (x.get("name") or x.get("title"))]
    except Exception as e:
        _log(f"  gumroad harvest failed: {e}")
    return []


def harvest_cosmos_library(page) -> List[Dict[str, Any]]:
    """Extract acquired assets from Cosmos by Leartes Studios."""
    try:
        raw = page.evaluate(_COSMOS_FETCH_JS)
        if isinstance(raw, list):
            _log(f"  cosmos harvest extracted {len(raw)} items")
            return [x for x in raw if isinstance(x, dict) and (x.get("name") or x.get("title"))]
    except Exception as e:
        _log(f"  cosmos harvest failed: {e}")
    return []


def _balanced_json_array(html: str, from_pos: int):
    """Return the balanced [...] array text starting at/after from_pos, or ''."""
    i = html.find("[", from_pos)
    if i == -1:
        return ""
    depth = 0
    in_str = False
    escape = False
    for j in range(i, len(html)):
        c = html[j]
        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if not in_str:
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    return html[i:j + 1]
    return ""




def fab_deep_media_pending() -> int:
    """Count canonical Fab listings not yet visited by the deep-media pass."""
    conn = get_connection()
    _ensure_fab_scan_marker(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE source='fab' "
        "AND store_url LIKE '%/library/assets/%' "
        "AND COALESCE(fab_deep_scanned, 0) = 0").fetchone()[0]
    conn.close()
    return n


def _ensure_fab_scan_marker(conn):
    """Add fab_deep_scanned tracking column if absent (0 = not yet visited)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(assets)")]
    if "fab_deep_scanned" not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN fab_deep_scanned INTEGER DEFAULT 0")
    conn.commit()


def run_fab_deep_media(limit: Optional[int] = None, cancel_event=None, progress=None) -> int:
    """SEPARATE long-running pass (like enrichment, NOT part of fetch):
    visits each Fab listing's canonical page once and pulls its full
    'medias' gallery + description. Progress is tracked per-row via the
    fab_deep_scanned column, so re-runs resume where the last stopped.

    Plain HTTP gets 403'd by Fab's bot wall; this rides the authed browser.
    Images/CSS/fonts aborted during navigation for speed."""
    from .db import get_connection

    conn = get_connection()
    _ensure_fab_scan_marker(conn)

    # Every canonical fab row not yet visited by this pass
    rows = conn.execute(
        "SELECT id, title, store_url FROM assets WHERE source='fab' "
        "AND store_url LIKE '%/library/assets/%' "
        "AND COALESCE(fab_deep_scanned, 0) = 0").fetchall()
    pending = [(r["id"], r["title"], r["store_url"]) for r in rows]
    if limit is not None and limit > 0:
        pending = pending[:limit]
    n_total_rows = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE source='fab' "
        "AND store_url LIKE '%/library/assets/%'").fetchone()[0]

    conn.close()

    already_done = n_total_rows - len(pending)
    if already_done > 0:
        _log(f"fab deep-media: {already_done} listings previously scanned; "
             f"{len(pending)} remaining")
    if not pending:
        if progress:
            progress(0, 0, "Fab galleries: nothing to do — every listing visited.")
        return 0

    total = len(pending)
    _log(f"fab deep-media: {total} listings to visit")

    cfg = load_config()
    proc, port = _launch_browser(cfg, "fab", start_url="about:blank", cdp=True)
    pw = browser = page = None
    enriched = 0
    route_on = False

    def _block_heavy(route):
        try:
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                return route.abort()
            return route.continue_()
        except Exception:
            pass

    try:
        pw, browser = _connect_cdp(port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        target = next((p for p in ctx.pages if (p.url or "") == "about:blank"), None)
        if target is None:
            target = ctx.new_page()
        else:
            for p in ctx.pages:
                if p is not target:
                    try: p.close()
                    except Exception: pass
        page = target
        try:
            page.route("**/*", _block_heavy)
            route_on = True
        except Exception:
            pass

        for idx, (aid, title, url) in enumerate(pending):
            if cancel_event is not None and cancel_event.is_set():
                _log("  fab deep-media cancelled.")
                break
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(600)
                html = page.content()
            except Exception as e:
                _log(f"  deep-media {idx + 1}/{total}: load failed ({e})")
                continue

            m = re.search(re.escape('"medias"'), html)
            if not m:
                continue                      # soft-404 / delisted: stays pending
            arr_txt = _balanced_json_array(html, m.start())
            if not arr_txt:
                continue
            try:
                medias = json.loads(arr_txt)
            except Exception:
                continue

            gallery = []
            videos = []
            for med in medias[:12]:
                imgs = [x for x in med.get("images", [])
                        if isinstance(x, dict) and x.get("url")]
                if imgs:
                    best = max(imgs, key=lambda x: x.get("width", 0))
                    u = best["url"]
                    g = "https:" + u if u.startswith("//") else u
                    if g not in gallery:
                        gallery.append(g)

            description = ""
            md = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{30,400})',
                           html, re.I)
            if md:
                description = md.group(1).replace("&amp;", "&")

            sets = ["gallery_images = ?", "image_url = CASE WHEN image_url = '' THEN ? ELSE image_url END",
                    "fab_deep_scanned = 1"]
            params: List[Any] = [json.dumps(gallery), gallery[0] if gallery else ""]
            if description:
                sets.append("summary = CASE WHEN summary = '' OR summary LIKE '%' || ': ' || title "
                            "OR lower(summary) = lower(title) THEN ? ELSE summary END")
                params.append(description)
            params.append(aid)

            item_conn = get_connection()
            item_conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id = ?", params)
            item_conn.commit()
            item_conn.close()

            enriched += 1
            if progress:
                progress(idx + 1, total,
                         f"Fab galleries {idx + 1}/{total} — {title[:40]}")
            if (idx + 1) % 10 == 0:
                _log(f"  fab deep-media: {idx + 1}/{total} ({enriched} galleries pulled)")
    finally:
        if route_on and page is not None:
            try:
                page.unroute("**/*", _block_heavy)
            except Exception:
                pass
        _close_browser_gracefully(proc, _profile_dir(cfg, "fab"))
        if browser:
            try: browser.close()
            except Exception: pass
        if pw:
            pw.stop()

    _log(f"fab deep-media done: {enriched}/{total} galleries pulled.")
    return enriched


def fetch_library(provider: str, cancel_event=None) -> int:
    """Open the store library page with the saved session (headed), intercept
    JSON responses, upsert discovered assets. Raises visible errors instead of
    failing silently."""
    cfg = load_config()
    if not has_saved_session(provider):
        raise RuntimeError(
            f"No {provider} browser profile found. Press Login first.")

    first_url = LIBRARY_URLS[provider][0]
    # CRITICAL: launch on about:blank and attach the interceptor BEFORE any
    # navigation — the store fires its asset GraphQL batches within the first
    # seconds of page load, and attaching afterwards misses them entirely.
    proc, port = _launch_browser(cfg, provider, start_url="about:blank", cdp=True)
    _log(f"{provider} fetch: browser pid={proc.pid} cdp={port}")

    pw = None
    browser = None
    harvested: List[Dict[str, Any]] = []
    seen_urls = set()
    all_json_urls: List[str] = []
    seen_sigs: set = set()
    captured_bodies: List[str] = []
    captured_headers: Dict[str, str] = {}
    landed_on_login = False
    owned_ids: set = set()
    fab_diag = {"logged": False}
    unity_diag = {"logged": False}

    def on_request(req):
        try:
            if "graphql/batch" in req.url and req.post_data:
                if len(captured_bodies) < 5:
                    captured_bodies.append(req.post_data)
                    _log(f"  captured graphql request template ({len(req.post_data)} bytes)")
                    # keep auth/CSRF headers so replays pass server checks
                    for hk, hv in (req.headers or {}).items():
                        lk = hk.lower()
                        if lk.startswith("x-") or lk in ("accept", "accept-language"):
                            captured_headers[hk] = hv
        except Exception:
            pass

    def on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            url = resp.url
            # Ignore background chatter from non-store origins (restored tabs,
            # newsfeeds): the generic list heuristic cannot tell them apart.
            if not _url_host_in_provider(url, provider):
                return
            if url not in seen_urls:
                seen_urls.add(url)
                all_json_urls.append(url)
            body = None
            parse_err = None
            try:
                body = resp.json()
            except Exception as e:
                parse_err = str(e)

            if body is None:
                _log(f"  [diag] JSON parse failed @ {url[:90]}: {parse_err}")
                return

            # Fab library feed: log its structure once per run so the native
            # harvester's field mappings stay verifiable against reality.
            if "fab.com/i/library/search" in url and not fab_diag["logged"]:
                fab_diag["logged"] = True
                try:
                    items = _fab_find_results(body)
                    _log(f"  [fab diag] top-level keys={list(body.keys())[:8]}")
                    if items:
                        sample = _fab_flatten(items[0])
                        _log(f"  [fab diag] item keys={sorted(str(k) for k in sample.keys())[:14]}")
                        _log("  [fab diag] sample=" + json.dumps(
                            {k: str(sample[k])[:48] for k in list(sample)[:10]}, ensure_ascii=False))
                except Exception as e:
                    _log(f"  [fab diag] error: {e}")

            # CurrentUser carries the AUTHORITATIVE owned-package id list
            try:
                if isinstance(body, list):
                    for part in body:
                        user = (part.get("data") or {}).get("user") or {}
                        raw = user.get("myAssets")
                        if raw:
                            ids = json.loads(raw) if isinstance(raw, str) else raw
                            if isinstance(ids, list):
                                owned_ids.update(str(i) for i in ids)
                                _log(f"  myAssets: {len(owned_ids)} owned package ids seen")
            except Exception:
                pass

            # Unity solr payloads: log one sample so we know whether keyImage,
            # slug and category fields are available for building valid URLs.
            if provider == "unity" and "assetstore" in url and not unity_diag["logged"]:
                lists = _looks_like_asset_lists(body)
                if lists:
                    unity_diag["logged"] = True
                    try:
                        item = lists[0][0]
                        _log(f"  [unity diag] item keys={sorted(str(k) for k in item.keys())[:24]}")
                        _log("  [unity diag] sample=" + json.dumps(
                            {k: str(item[k])[:60] for k in list(item)[:12]}, ensure_ascii=False))
                    except Exception as e:
                        _log(f"  [unity diag] error: {e}")

            lists = _looks_like_asset_lists(body)
            if not lists and "api.cosmos.leartesstudios.com/inventory" in url and isinstance(body, dict) and "data" in body:
                raw_data = body.get("data")
                if isinstance(raw_data, list):
                    lists = [raw_data]
            if not lists and "graphql" in url:
                # log structural keys only without dumping user identity/email/PII
                keys_summary = ""
                if isinstance(body, dict):
                    keys_summary = f"keys={list(body.keys())[:6]}"
                elif isinstance(body, list) and body and isinstance(body[0], dict):
                    keys_summary = f"list of dicts, entry keys={list(body[0].keys())[:6]}"
                _log(f"  [diag] graphql batch w/o recognizable lists ({keys_summary})")

            for lst in lists:
                # reject UI-facet payloads (platform/tag/license filters): dicts with count,
                # displayCount, or missing real product fields (url, seller, thumbnail, description)
                items = []
                for x in lst:
                    if not isinstance(x, dict):
                        continue
                    # Facet marker keys
                    if any(k in x for k in ("count", "displayCount", "doc_count", "selected", "facetCount")):
                        continue
                    # Must have at least a listing URL, store URL, publisher, keyImage, or substantial description
                    has_asset_markers = any(bool(x.get(k)) for k in (
                        "url", "listingUrl", "sellerName", "publisher", "keyImage",
                        "thumbnail", "images", "description", "aiDescription", "assetType",
                        "slug", "cover_image", "coverImage", "subtitle", "license"
                    ))
                    if not has_asset_markers:
                        continue
                    items.append(x)

                if not items:
                    continue
                sig = f"{url}|{len(lst)}|{items[0].get('name', '')[:30]}"
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                _log(f"  candidate list @ {url[:110]} "
                     f"({len(lst)} items, keys={sorted(list(lst[0].keys()))[:8]})")
                harvested.extend(items)
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
        ctx.on("request", on_request)

        # Prefer OUR about:blank tab (created by _launch_browser); restored
        # session tabs would otherwise receive the navigation, leaving the
        # blank tab dangling and feeding newsfeed noise into the interceptor.
        target = None
        for p in ctx.pages:
            u = (p.url or "")
            if u == "about:blank":
                target = p
                break
        if target is None:
            target = ctx.new_page()
        else:
            for p in ctx.pages:
                if p is not target:
                    try:
                        p.close()
                    except Exception:
                        pass
        page = target
        try:
            page.goto(first_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            _log(f"warn: goto {first_url}: {e}")
        page.wait_for_timeout(5000)
        check_urls_for_login_redirect()

        # ---- Early authoritative native harvest:
        # Gumroad renders via Inertia.js with pagination across all pages in seconds.
        if provider == "gumroad" and not landed_on_login:
            try:
                gumroad_items = harvest_gumroad_library(page)
                harvested.extend(gumroad_items)
                _log(f"  gumroad native harvest returned {len(gumroad_items)} items")
            except Exception as e:
                _log(f"  gumroad native harvest failed: {e}")

        # Cosmos: fetches all inventory pages directly via API in seconds
        if provider == "cosmos" and not landed_on_login:
            try:
                cosmos_items = harvest_cosmos_library(page)
                harvested.extend(cosmos_items)
                _log(f"  cosmos native harvest returned {len(cosmos_items)} items")
            except Exception as e:
                _log(f"  cosmos native harvest failed: {e}")

        # Scroll to bottom repeatedly until content stops growing: the page
        # lazy-loads more GraphQL batches as you reach the end. JS scrolling
        # works where synthetic wheel events rubber-band without scrolling.
        if not ((provider in ("gumroad", "cosmos")) and harvested):
            stable_rounds = 0
            last_sig = None
            login_streak = 0
            started = time.time()
            for i in range(120):
                if cancel_event is not None and cancel_event.is_set():
                    _log(f"{provider} fetch cancelled.")
                    break
                check_urls_for_login_redirect()
                if landed_on_login:
                    login_streak += 1
                    # SSO chains pass auth-looking URLs transiently; only trust
                    # the verdict after several consecutive polls, and never
                    # abort within the first 15s of the redirect dance
                    if login_streak >= 4 and time.time() - started > 15:
                        _log("  looks like we landed on a login page; stopping.")
                        break
                else:
                    login_streak = 0
                try:
                    where = page.evaluate(_SCROLL_JS)
                    page.wait_for_timeout(1500)
                    sig = f"{len(harvested)}|{page.evaluate('document.documentElement.scrollHeight')}"
                    if i % 10 == 0:
                        _log(f"  scroll round {i}: via={where} harvested={len(harvested)} sig={sig}")
                    if sig == last_sig:
                        stable_rounds += 1
                        if stable_rounds >= 10:
                            break
                    else:
                        stable_rounds = 0
                    last_sig = sig
                except Exception as e:
                    _log(f"  scroll stopped: {e}")
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
        # ---- chunked GraphQL harvest: build our OWN SearchResults queries
        # from the authoritative myAssets id list, mimicking the page's exact
        # request shape (limitedIds chunks + csrf header from the capture).
        new_titles = {str(i.get("name") or i.get("title") or "") for i in harvested}

        def harvest_text(text):
            n = 0
            try:
                for lst in _looks_like_asset_lists(json.loads(text)):
                    for item in lst:
                        t = str(item.get("name") or item.get("title") or "")
                        if not t or t in new_titles:
                            continue
                        if set(item.keys()) <= {"__typename", "count", "name", "id"}:
                            continue
                        new_titles.add(t)
                        harvested.append(item)
                        n += 1
            except Exception:
                pass
            return n

        js_fetch = """
        async (args) => {
            const r = await fetch('/api/graphql/batch', {
                method: 'POST',
                headers: args.headers,
                credentials: 'include',
                body: args.body
            });
            return await r.text();
        }
        """

        if owned_ids and captured_bodies and not landed_on_login:
            # find the SearchResults template body
            template = None
            for b in captured_bodies:
                if "searchPackageFromSolr" in b:
                    template = b
                    break
            if not template:
                _log("[diag] no SearchResults request captured; cannot build chunk queries")
            else:
                try:
                    ops = json.loads(template)
                    # locate variables dict
                    variables = None
                    for op in ops:
                        if isinstance(op, dict) and isinstance(op.get("variables"), dict):
                            variables = op["variables"]
                            break
                    if variables is None:
                        raise ValueError("no variables found in captured request body")

                    # reuse csrf-ish headers observed on the page's own requests
                    hdrs = {k: v for k, v in (captured_headers or {}).items()
                            if k.lower().startswith("x-")
                            or k.lower() in ("accept", "accept-language")}
                    _log(f"  chunk harvest: replay headers {sorted(hdrs.keys())}")

                    ids_sorted = sorted(owned_ids)
                    chunk_size = 42          # matches Unity's own chunking
                    rows = 48                # ask for more per page; server may cap
                    total_chunks = (len(ids_sorted) + chunk_size - 1) // chunk_size
                    _log(f"  chunk harvest: {len(ids_sorted)} ids -> {total_chunks} chunks")

                    detailed_ids = set()
                    for ci in range(0, len(ids_sorted), chunk_size):
                        chunk = ids_sorted[ci:ci + chunk_size]
                        q_val = "limitedIds:" + "\\".join(chunk)
                        stale = 0
                        pagenum = 0
                        while stale < 2 and pagenum < 10:
                            trial = json.loads(template)
                            for op in trial:
                                if isinstance(op, dict) and isinstance(op.get("variables"), dict):
                                    v = op["variables"]
                                    v["q"] = [q_val] + [
                                        e for e in v.get("q", [])
                                        if not str(e).startswith("limitedIds:")
                                        and str(e) != "on_sale:true"]
                                    v["page"] = pagenum
                                    v["rows"] = rows
                            try:
                                text = page.evaluate(js_fetch, {
                                    "body": json.dumps(trial),
                                    "headers": hdrs,
                                })
                            except Exception as e:
                                _log(f"  chunk harvest stopped (page eval): {e}")
                                stale = 99
                                break

                            # One-time schema capture: save the first replayed
                            # response so field mappings (cover art, slugs, URLs)
                            # are built from evidence instead of guesses.
                            _sample_path = os.path.join(ROOT_DIR, "data", "unity_solr_sample.json")
                            if text and not os.path.exists(_sample_path):
                                try:
                                    with open(_sample_path, "w", encoding="utf-8") as _sf:
                                        _sf.write(text[:200_000])
                                    _log("  [diag] saved first solr response -> data/unity_solr_sample.json")
                                except Exception:
                                    pass
                            n = harvest_text(text)
                            _log(f"  chunk {ci // chunk_size + 1}/{total_chunks} "
                                 f"page {pagenum}: +{n} new (total {len(harvested)})")
                            if n == 0:
                                stale += 1
                            else:
                                stale = 0
                            pagenum += 1
                        detailed_ids.update(chunk)

                    # reconciliation: owned ids we hold no details for
                    import sqlite3
                    conn = sqlite3.connect(DB_PATH)
                    known = {str(r[0]) for r in conn.execute(
                        "SELECT package_id FROM assets WHERE source='unity' AND package_id != ''")}
                    missing = sorted(owned_ids - known)
                    _log(f"  ownership reconciliation: {len(owned_ids)} owned, "
                         f"{len(missing)} not present in vault details")
                    if missing:
                        _log(f"  missing sample: {missing[:25]}")
                    conn.close()
                except Exception as e:
                    _log(f"  chunk harvest failed: {e}")


        # ---- Fab native harvest: /i/library/search is plain REST with cursor
        # pagination — replay until exhausted for authoritative completeness.
        if provider == "fab" and not landed_on_login:
            try:
                fab_items = harvest_fab_library(page)
                harvested.extend(fab_items)
                _log(f"  fab native harvest returned {len(fab_items)} items")
            except Exception as e:
                _log(f"  fab native harvest failed: {e}")

        # ---- Gumroad native harvest (fallback if early harvest didn't run)
        if provider == "gumroad" and not landed_on_login and not harvested:
            try:
                gumroad_items = harvest_gumroad_library(page)
                harvested.extend(gumroad_items)
                _log(f"  gumroad native harvest returned {len(gumroad_items)} items")
            except Exception as e:
                _log(f"  gumroad native harvest failed: {e}")

        # ---- Cosmos native harvest (fallback if early harvest didn't run)
        if provider == "cosmos" and not landed_on_login and not harvested:
            try:
                cosmos_items = harvest_cosmos_library(page)
                harvested.extend(cosmos_items)
                _log(f"  cosmos native harvest returned {len(cosmos_items)} items")
            except Exception as e:
                _log(f"  cosmos native harvest failed: {e}")

        time.sleep(2)   # let final cookie writes flush before closing
    finally:
        _close_browser_gracefully(proc, _profile_dir(cfg, provider))
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
    processed_titles: set = set()
    for item in harvested:
        title = None
        for k in _NAME_KEYS:
            if item.get(k):
                title = str(item[k]).strip()
                break
        if not title:
            unknown += 1
            continue
        pkg_id = str(item.get("uid") or item.get("id") or item.get("listingId") or item.get("packageId") or "").strip()
        dedup_key = (pkg_id, title) if pkg_id else title
        if dedup_key in processed_titles:
            continue
        processed_titles.add(dedup_key)

        store_url = _extract_store_url(item, provider)
        publisher = _extract_publisher(item)

        # Fab: canonical listing URL from the library-entry uid stashed at harvest.
        if provider == "fab" and item.get("_lib_uid"):
            store_url = f"https://www.fab.com/library/assets/{item['_lib_uid']}"

        # Prefer the category-path URL scheme (verified working 2026-08):
        # https://assetstore.unity.com/packages/<category>/<anything>-<id>
        # The slug text is irrelevant; category + id resolve to the real page.
        if provider == "unity" and pkg_id:
            cat = str(item.get("category") or "").strip()
            if cat and "/" in cat:
                store_url = f"https://assetstore.unity.com/packages/{cat}/x-{pkg_id}"

        # Reject pure UI filter facet stubs (no package_id, no store_url, and no publisher)
        if not pkg_id and not store_url and not publisher:
            unknown += 1
            continue

        cls = classify_asset(title)
        cover_img, gallery_imgs = _extract_media_images(item)
        # Deep-scan results win: full screenshot galleries from the listing page.
        deep_gal = item.get("_deep_gallery") or []
        if deep_gal:
            gallery_imgs = deep_gal
            if not cover_img and gallery_imgs:
                cover_img = gallery_imgs[0]
        stable_id = _stable_id(provider, pkg_id, title)
        is_already_enriched = bool(cover_img and provider in ("gumroad", "cosmos", "fab"))
        asset = {
            "id": stable_id,
            "source": provider,
            "package_id": pkg_id,
            "title": title,
            "publisher": publisher or ("Leartes Studios" if provider == "cosmos" else ""),
            "version": str(item.get("versionName") or item.get("version") or item.get("packageVersion") or ""),
            "claimed_date": (item.get("createdAt") or item.get("acquiredAt") or "")[:10],
            "store_url": store_url,
            "image_url": cover_img,
            **cls,
            "gallery_images": gallery_imgs,
            "video_links": [],
            "enriched": 1 if is_already_enriched else 0,
        }
        # prefer the store's real description over the heuristic one
        tech_specs = ""
        if isinstance(item.get("assetFormats"), list) and item["assetFormats"]:
            tech_specs = item["assetFormats"][0].get("technicalSpecs", {}).get("technicalDetails", "")
        rich = re.sub(r"<[^>]+>", " ", str(item.get("description")
                                          or item.get("aiDescription")
                                          or tech_specs or ""))
        rich = re.sub(r"\s+", " ", rich).strip()
        if len(rich) > 60:
            asset["summary"] = rich[:800]
        elif item.get("_deep_summary"):
            asset["summary"] = str(item["_deep_summary"])[:800]
        elif rich:
            asset["summary"] = rich[:800]
        if item.get("variants"):
            asset["usage_notes"] = f"Variant / License: {item['variants']}"
        elif item.get("license"):
            asset["usage_notes"] = f"License: {item['license']}"
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

# Structured media array embedded in listing pages:
#   "images":[{"type":"screenshot","imageUrl":"//…cdn…/package-screenshot/<guid>_scaled.jpg",…}, …]
# Far more reliable than sweeping every key-image ref on the page, which
# includes recommendation cards for OTHER assets.
_SCREENSHOT_RE = re.compile(
    r'\{"type":"screenshot","imageUrl":"((?:https?:)?//[^" ]+?(?:package-screenshot|key-image)/[^"]+?)"', re.I)
_YT_EMBED_RE = re.compile(r'"type":"youtube","imageUrl":"(?:https?:)?//www\.youtube\.com/embed/([\w-]{11})')

# Unity soft-404 shells (dead listing URLs) expose only the site-wide logo
# as og:image — storing it gives every asset the same useless "cover".
_GENERIC_LOGO_MARKERS = ("/images/logo.png", "/cdn-origin/images/logo")


def count_unenriched(db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    n = conn.execute("SELECT COUNT(*) FROM assets WHERE enriched = 0").fetchone()[0]
    conn.close()
    return n


def _enrich_one(asset):
    """Enrich a single asset with a dedicated thread-safe HTTP request. Returns True when marked done."""
    if asset.get("source") in ("gumroad", "cosmos", "fab"):
        mark_enriched(asset["id"])
        return True

    url = asset.get("store_url") or ""
    if not url.startswith("http"):
        pkg_id = str(asset.get("package_id") or "").strip()
        if pkg_id and pkg_id.isdigit() and asset.get("source") == "unity":
            url = f"https://assetstore.unity.com/packages/slug/{pkg_id}"
        else:
            mark_enriched(asset["id"])   # nothing to fetch; don't retry forever
            return True

    if "packages/slug-" in url:
        url = url.replace("packages/slug-", "packages/slug/")

    try:
        with httpx.Client(follow_redirects=True, timeout=15,
                          headers={"User-Agent": "Mozilla/5.0 (Quartermaster enrichment)"}) as client:
            r = client.get(url)
            html = r.text
            canonical_url = str(r.url) if (str(r.url).startswith("http") and "assetstore.unity.com" in str(r.url)) else url
    except Exception as e:
        _log(f"warn: {asset.get('title')}: {e}")
        return False

    image_url = ""
    m = _OG_IMAGE_RE.search(html)
    if m:
        candidate = m.group(1).replace("&amp;", "&")
        if not any(marker in candidate for marker in _GENERIC_LOGO_MARKERS):
            image_url = candidate

    if not image_url:
        m_key = re.search(r'https?://assetstorev1-prd-cdn\.unity3d\.com/key-image/[0-9a-f-]+\.(?:png|jpg|jpeg|webp)', html, re.I)
        if m_key:
            image_url = m_key.group(0)

    # ---- Scope extraction to THIS package's media array. ----
    # Listing pages embed ~95 "images" arrays: one for the product itself,
    # the rest for recommendation cards (each with its own price/media).
    # Sweeping the whole document grabs whichever array comes first — always
    # the sitewide featured strip. The array following the product's own
    # "id":"<pid>" marker belongs to it (verified: exactly one such array).
    own_media = ""
    pid = str(asset.get("package_id") or "")
    if pid:
        id_pos = -1
        for pat in (f'"id":"{pid}"', f'"id": {pid}', f'"id":{pid}'):
            mm = re.search(re.escape(pat), html)
            if mm:
                id_pos = mm.start()
                break
        if id_pos >= 0:
            arr = re.search(r'"images"\s*:\s*\[', html[id_pos:])
            if arr:
                i = html.index("[", id_pos + arr.start())
                depth, j = 0, i
                while j < len(html):
                    c = html[j]
                    if c == '"':                      # skip bracket chars inside strings
                        j = html.find('"', j + 1)
                    elif c == '[':
                        depth += 1
                    elif c == ']':
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                own_media = html[i:j + 1]

    gallery = []

    def _norm_img(u: str) -> str:
        return "https:" + u if u.startswith("//") else u

    # Preferred: the product's own typed media array (screenshots only).
    for u in _SCREENSHOT_RE.findall(own_media):
        g = _norm_img(u)
        if g not in gallery:
            gallery.append(g)
        if len(gallery) >= 12:
            break

    # Fallback: legacy CDN sweep (older cached pages / layout variants).
    if not gallery:
        for g in re.findall(r'https?://assetstorev1-prd-cdn\.unity3d\.com/(?:package-screenshot|key-image)/[0-9a-f-]+\.(?:png|jpg|jpeg|webp)', html, re.I):
            if g not in gallery:
                gallery.append(g)
            if len(gallery) >= 8:
                break

    videos = []
    # Videos scoped to the product's own media array — sitewide sweeps picked
    # up recommendation cards' trailers.
    if own_media:
        for vid in _YT_EMBED_RE.findall(own_media):
            link = f"https://www.youtube.com/watch?v={vid}"
            if link not in videos:
                videos.append(link)
        for v in _VIDEO_HINTS.findall(own_media)[:3]:
            v = v.replace("\\/", "/")
            if v not in videos:
                videos.append(v)
    else:
        # Legacy fallback for pages where the own-id marker wasn't found.
        for vid in _YT_RE.findall(html)[:3]:
            link = f"https://www.youtube.com/watch?v={vid}"
            if link not in videos:
                videos.append(link)
    videos = videos[:5]

    desc = ""
    md = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{30,400})["\']', html, re.I)
    if md:
        desc = md.group(1).replace("&amp;", "&")

    mark_enriched(
        asset["id"],
        image_url=image_url,
        gallery_images=gallery,
        video_links=videos,
        summary=(desc or "")[:400],
    )
    return True


def enrich_assets(limit: Optional[int] = None, progress=None,
                  cancel_event=None) -> int:
    """
    Fetch store pages for un-enriched assets in BATCHES with pauses between
    them (polite to remote servers), extracting cover art, gallery images,
    video LINKS, and description. Assets inside a batch download in parallel
    (config: enrich_concurrency).
      limit=None -> sweep the ENTIRE backlog
      progress   -> optional callable(done, total, text) for live UI updates
      cancel_event -> optional threading.Event; set to stop gracefully
    Returns number enriched.
    """
    import time as _time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cfg = load_config()
    batch_size = int(cfg["enrich_batch_size"])
    pause = int(cfg.get("enrich_batch_pause", 3))
    workers = max(1, int(cfg.get("enrich_concurrency", 4)))
    total_backlog = count_unenriched()
    if total_backlog == 0:
        _log("enrichment: nothing to do.")
        if progress:
            progress(0, 0, "Enrichment: nothing to do — vault is fully enriched.")
        return 0

    done = 0
    target = min(total_backlog if limit is None else limit, total_backlog)
    cancelled = False

    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()

    while done < target and not is_cancelled():
        assets = get_unenriched(batch_size)[:target - done]
        if not assets:
            break

        with ThreadPoolExecutor(max_workers=min(workers, len(assets))) as ex:
            futures = [ex.submit(_enrich_one, a) for a in assets]
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        done += 1
                except Exception:
                    pass
                if progress:
                    remaining = count_unenriched()
                    try:
                        conn = get_connection()
                        row = conn.execute(
                            "SELECT COUNT(*) AS t, COALESCE(SUM(enriched),0) AS e FROM assets").fetchone()
                        conn.close()
                        vault_total, vault_enriched = row["t"], row["e"]
                    except Exception:
                        vault_total = vault_enriched = 0
                    progress(done, target,
                             f"Enriching… {done}/{target} this run · "
                             f"vault: {vault_enriched}/{vault_total} enriched · {remaining} pending")
                if is_cancelled():
                    break

        # pause between batches so remote servers see a human-ish cadence
        if done < target and count_unenriched() > 0 and pause > 0 and not is_cancelled():
            if progress:
                progress(done, target, f"Pausing {pause}s between batches (politeness mode)…")
                steps = int(pause * 10)
                for _ in range(steps):
                    if is_cancelled():
                        break
                    _time.sleep(0.1)

    status = "cancelled" if is_cancelled() else "finished"
    _log(f"enrichment {status}: {done} assets processed.")
    if progress:
        progress(done, target, f"Enrichment {status}: {done} processed · "
                 f"{count_unenriched()} left in backlog (run again anytime).")
    return done


def sync_quixel_catalog(cancel_event=None, progress=None, db_path=DB_PATH) -> Dict[str, Any]:
    """
    Syncs the complete Quixel Megascans & Megaplants catalog from Fab.
    Sellers: 'Quixel Megascans' and 'Quixel Megaplants'.
    Paginates through all listings and upserts into local vault database.
    """
    import httpx
    import time
    try:
        from . import ingest, config, db
    except ImportError:
        import ingest, config, db
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    sellers = ["Quixel Megascans", "Quixel Megaplants"]
    total_added = 0
    total_updated = 0
    page_count = 0
    failed_sellers = []

    client = httpx.Client(headers=headers, timeout=25.0)
    try:
        for seller in sellers:
            if cancel_event and cancel_event.is_set():
                break
            encoded_seller = seller.replace(" ", "+")
            url = f"https://www.fab.com/i/listings/search?seller={encoded_seller}&count=50"
            _log(f"Starting Quixel sync for '{seller}'...")

            while url:
                if cancel_event and cancel_event.is_set():
                    _log("Quixel sync cancelled by user.")
                    break

                data = None
                for attempt in range(4):
                    try:
                        r = client.get(url)
                        if r.status_code in (403, 429):
                            backoff = (attempt + 1) * 2.0
                            _log(f"  [rate-limit] Quixel sync HTTP {r.status_code}, backing off {backoff}s...")
                            if cancel_event and cancel_event.is_set():
                                break
                            time.sleep(backoff)
                            continue
                        if r.status_code != 200:
                            _log(f"  [warn] Quixel sync HTTP {r.status_code} for {url}")
                            break
                        data = r.json()
                        break
                    except Exception as e:
                        _log(f"  [warn] Quixel fetch attempt {attempt+1} failed: {e}")
                        time.sleep(1.0)

                if not data or not isinstance(data, dict):
                    failed_sellers.append(seller)
                    _log(f"  [error] Quixel sync stopped prematurely for '{seller}' (rate-limited or network error).")
                    break

                items = data.get("results", [])
                if not items:
                    break

                for raw in items:
                    uid = raw.get("uid")
                    title = str(raw.get("title") or raw.get("name") or "").strip()
                    if not uid or not title:
                        continue

                    # Extract tags and category for accurate classification
                    raw_tags = [t.get("name") for t in raw.get("tags", []) if isinstance(t, dict) and t.get("name")]
                    cat_name = raw.get("category", {}).get("name") or raw.get("category", {}).get("slug") or ""
                    if cat_name and cat_name not in raw_tags:
                        raw_tags.append(cat_name)
                    for default_tag in ("Quixel", "Megascans" if "Megascans" in seller else "Megaplants"):
                        if default_tag not in raw_tags:
                            raw_tags.append(default_tag)

                    desc = raw.get("description") or f"{title} by {seller}"
                    classified = ingest.classify_asset(
                        title=title,
                        publisher=seller,
                        tags_list=raw_tags,
                        summary_text=desc
                    )

                    # Extract cover image and distinct gallery images (avoiding multiple resolution crops of the same image)
                    thumbs = raw.get("thumbnails", [])
                    cover_url = ""
                    gallery = []
                    if thumbs and isinstance(thumbs, list):
                        for th in thumbs:
                            m_url = th.get("mediaUrl")
                            if m_url and config.is_safe_image_url(m_url):
                                if not cover_url:
                                    cover_url = m_url
                                elif m_url not in gallery and m_url != cover_url:
                                    gallery.append(m_url)

                    record = {
                        "id": f"quixel_{uid}",
                        "source": "quixel",
                        "package_id": uid,
                        "product_id": "",
                        "title": title,
                        "publisher": seller,
                        "publisher_id": raw.get("user", {}).get("sellerId", ""),
                        "version": "",
                        "size_mb": 0.0,
                        "size_str": "",
                        "claimed_date": raw.get("publishedAt", "")[:10] if raw.get("publishedAt") else "",
                        "store_url": f"https://www.fab.com/listings/{uid}",
                        "category": classified.get("category", "3D Environments & Props"),
                        "render_pipelines": classified.get("render_pipelines", ["HDRP", "URP", "Built-in"]),
                        "tags": list(set(raw_tags + classified.get("tags", []))),
                        "summary": classified.get("summary") or desc,
                        "usage_notes": "",
                        "image_url": cover_url,
                        "gallery_images": gallery[:10],
                        "video_links": [],
                        "formats": ["Unreal Engine", "FBX", "Textures"],
                        "license": "Epic Content License (Free Claim)",
                        "enriched": 1
                    }

                    res = db.upsert_asset(record, db_path=db_path)
                    if res == "inserted":
                        total_added += 1
                    else:
                        total_updated += 1

                page_count += 1
                if progress:
                    progress(total_added + total_updated, None, f"Synced {total_added + total_updated} Quixel assets (Page {page_count})…")
                _log(f"  Quixel sync: page {page_count} (+{len(items)} items, {total_added + total_updated} total)")

                url = data.get("next")
                time.sleep(0.12)  # Polite cadence between pages to avoid Cloudflare rate triggers
    finally:
        client.close()

    status = "partial" if failed_sellers else ("cancelled" if (cancel_event and cancel_event.is_set()) else "completed")
    _log(f"Quixel sync {status}: {total_added} added, {total_updated} updated across {page_count} pages.")
    return {
        "status": status,
        "added": total_added,
        "updated": total_updated,
        "total_synced": total_added + total_updated,
        "pages": page_count,
        "failed_sellers": failed_sellers
    }


def enrich_quixel_specs(limit: Optional[int] = None, cancel_event=None, progress=None, db_path=DB_PATH) -> Dict[str, Any]:
    """
    Enrich Quixel Megascans & Megaplants catalog rows with physical scan specs
    (texel density, scan area / physical dimensions, and map lists) directly
    from Fab's public listing endpoint without requiring asset downloads.
    """
    import httpx
    import time
    try:
        from . import config, db
        from .local_scan import _parse_scan_specs_from_text
    except ImportError:
        import config, db
        from local_scan import _parse_scan_specs_from_text

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.fab.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"
    }

    conn = db.get_connection(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, package_id, title, summary, usage_notes, tags, formats
        FROM assets
        WHERE source = 'quixel' AND (
            usage_notes IS NULL
            OR (usage_notes NOT LIKE '%Texel density%' AND usage_notes NOT LIKE '%Maps:%' AND usage_notes NOT LIKE '%Scan area:%')
        )
        ORDER BY rowid ASC
    """)
    rows = cur.fetchall()
    if limit is not None and limit > 0:
        rows = rows[:limit]

    total_to_enrich = len(rows)
    if total_to_enrich == 0:
        _log("Quixel scan specs enrichment: all Quixel assets already have scan specs.")
        if progress:
            progress(0, 0, "Quixel specs: all assets already enriched.")
        conn.close()
        return {"status": "completed", "enriched": 0, "total": 0}

    _log(f"Starting Quixel scan specs enrichment for {total_to_enrich} assets...")
    enriched_count = 0
    client = httpx.Client(headers=headers, follow_redirects=True, timeout=15.0)

    try:
        for idx, (aid, pkg_id, title, old_sum, old_use, old_tags_json, old_fmt_json) in enumerate(rows):
            if cancel_event and cancel_event.is_set():
                _log("Quixel specs enrichment cancelled by user.")
                break

            target_uid = (pkg_id or aid.replace("quixel_", "")).strip()
            if not target_uid:
                continue

            url = f"https://www.fab.com/i/listings/{target_uid}"
            desc = ""
            for attempt in range(3):
                try:
                    r = client.get(url)
                    if r.status_code in (403, 429):
                        backoff = (attempt + 1) * 2.0
                        time.sleep(backoff)
                        continue
                    if r.status_code == 200:
                        data = r.json()
                        desc = data.get("description") or ""
                    break
                except Exception:
                    time.sleep(0.5)

            if desc:
                specs = _parse_scan_specs_from_text(desc)
                if specs:
                    try:
                        curr_tags = json.loads(old_tags_json or "[]")
                    except Exception:
                        curr_tags = []

                    notes_parts = []
                    if specs.get("texel_density"):
                        notes_parts.append(f"Texel density: {specs['texel_density']}")
                    if specs.get("scan_area"):
                        notes_parts.append(f"Scan area: {specs['scan_area']}")
                    if specs.get("maps"):
                        notes_parts.append(f"Maps: {', '.join(specs['maps'])}")
                        for map_name in specs["maps"]:
                            ml = map_name.lower()
                            if ml in ("displacement", "roughness", "normal", "cavity", "ao", "specular", "fuzz") and ml not in curr_tags:
                                curr_tags.append(ml)
                    if specs.get("displacement_scale"):
                        notes_parts.append(f"Displacement scale: {specs['displacement_scale']}")

                    new_use = old_use or ""
                    if notes_parts:
                        spec_note = " · ".join(notes_parts)
                        if "Texel density:" not in new_use:
                            new_use = f"{spec_note}\n{new_use}".strip() if new_use else spec_note

                    new_sum = old_sum or ""
                    spec_details = []
                    if specs.get("scan_area"):
                        spec_details.append(specs["scan_area"])
                    if specs.get("texel_density"):
                        spec_details.append(specs["texel_density"])
                    if spec_details:
                        spec_str = f"({', '.join(spec_details)})"
                        if not new_sum.endswith(spec_str):
                            base_sum = re.sub(r"\s*\((?:[^)]*px/m|[^)]*\d+x\d+\s*m)[^)]*\)$", "", new_sum)
                            new_sum = f"{base_sum} {spec_str}".strip() if base_sum else spec_str

                    cur.execute("""
                        UPDATE assets 
                        SET usage_notes = ?, summary = ?, tags = ?
                        WHERE id = ?
                    """, (new_use, new_sum, json.dumps(curr_tags), aid))
                    db._sync_fts(cur, aid)
                    enriched_count += 1

            if (idx + 1) % 25 == 0:
                conn.commit()
                if progress:
                    progress(enriched_count, total_to_enrich, f"Enriching Quixel specs… ({enriched_count}/{total_to_enrich})")
                _log(f"  Quixel specs: {enriched_count}/{idx + 1} enriched…")

            time.sleep(0.06)

        conn.commit()
    finally:
        client.close()
        conn.close()

    status = "cancelled" if (cancel_event and cancel_event.is_set()) else "completed"
    _log(f"Quixel specs enrichment {status}: {enriched_count}/{total_to_enrich} assets updated.")
    if progress:
        progress(enriched_count, total_to_enrich, f"Quixel specs enrichment {status}: {enriched_count} updated.")
    return {
        "status": status,
        "enriched": enriched_count,
        "total": total_to_enrich
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "login" and len(sys.argv) > 2:
        interactive_login(sys.argv[2])
    elif cmd == "fetch" and len(sys.argv) > 2:
        fetch_library(sys.argv[2])
    elif cmd in ("quixel", "sync-quixel"):
        sync_quixel_catalog()
    elif cmd in ("enrich-quixel", "enrich-specs", "quixel-specs"):
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        enrich_quixel_specs(n)
    elif cmd == "enrich":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else None
        enrich_assets(n)
    elif cmd == "fab-deep-media":
        run_fab_deep_media()
    else:
        print("Usage:\n  python -m src.store_client login <unity|fab>\n"
              "  python -m src.store_client fetch <unity|fab>\n"
              "  python -m src.store_client sync-quixel\n"
              "  python -m src.store_client enrich-quixel [limit]\n"
              "  python -m src.store_client enrich [limit]\n"
              "  python -m src.store_client fab-deep-media [limit]")
