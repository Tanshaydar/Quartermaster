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
    from .db import (get_unenriched, mark_enriched, upsert_asset, DB_PATH,
                     get_connection)
    from .config import load_config
    from .ingest import classify_asset
except ImportError:
    from db import (get_unenriched, mark_enriched, upsert_asset, DB_PATH,
                    get_connection)
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

LIBRARY_URLS = {
    # NOTE: /purchases and /account/purchases are dead (404) as of 2026-08.
    # /account/downloads redirects here; this is the owned-packages page.
    "unity": ["https://assetstore.unity.com/account/assets"],
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

    def on_request(req):
        try:
            if "graphql/batch" in req.url and req.post_data:
                if len(captured_bodies) < 5:
                    captured_bodies.append(req.post_data)
                    _log(f"  captured graphql request ({len(req.post_data)} bytes)")
                    _log(f"  body[:1200]: {req.post_data[:1200]}")
                    _log(f"  body[-800:]: {req.post_data[-800:]}")   # variables live at the tail
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

            lists = _looks_like_asset_lists(body)
            if not lists and "graphql" in url:
                # keep evidence of unrecognized batch payloads for matching
                snippet = json.dumps(body)[:400] if not isinstance(body, str) else body[:400]
                _log(f"  [diag] graphql batch w/o recognizable lists "
                     f"({len(json.dumps(body))} bytes): {snippet}")

            for lst in lists:
                # reject UI-facet payloads (platform/tag filters): tiny dicts
                # with only name/count/__typename keys
                items = [x for x in lst
                         if not (set(x.keys()) <= {"__typename", "count", "name", "id"})]
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

        pages = ctx.pages
        page = pages[0] if pages else ctx.new_page()
        try:
            page.goto(first_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            _log(f"warn: goto {first_url}: {e}")
        page.wait_for_timeout(5000)

        # Scroll to bottom repeatedly until content stops growing: the page
        # lazy-loads more GraphQL batches as you reach the end. JS scrolling
        # works where synthetic wheel events rubber-band without scrolling.
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
        # prefer the store's real description over the heuristic one
        rich = re.sub(r"<[^>]+>", " ", str(item.get("description")
                                          or item.get("aiDescription") or ""))
        rich = re.sub(r"\s+", " ", rich).strip()
        if len(rich) > 60:
            asset["summary"] = rich[:800]
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


def count_unenriched(db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    n = conn.execute("SELECT COUNT(*) FROM assets WHERE enriched = 0").fetchone()[0]
    conn.close()
    return n


def _enrich_one(client, asset):
    """Enrich a single asset. Returns True when marked done."""
    url = asset.get("store_url") or ""
    if not url.startswith("http"):
        mark_enriched(asset["id"])   # nothing to fetch; don't retry forever
        return True
    try:
        r = client.get(url)
        html = r.text
    except Exception as e:
        _log(f"warn: {asset['title']}: {e}")
        return False

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
    target = min(limit or total_backlog, total_backlog)
    cancelled = False

    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()

    with httpx.Client(follow_redirects=True, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0 (VaultMCP enrichment)"}) as client:
        while done < target and not is_cancelled():
            assets = get_unenriched(batch_size)[:target - done]
            if not assets:
                break

            with ThreadPoolExecutor(max_workers=min(workers, len(assets))) as ex:
                futures = [ex.submit(_enrich_one, client, a) for a in assets]
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
