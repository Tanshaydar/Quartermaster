"""
VaultMCP local web server & REST API.

  python -m src.server            # serves on http://localhost:7890

Endpoints:
  GET  /                          -> dark-mode search UI (sets SameSite session cookie)
  GET  /api/assets                -> search/filter (query, category, pipeline, source, limit, offset)
  GET  /api/asset/{id}            -> full details
  GET  /api/stats                 -> counts by category/source
  GET  /api/recipes               -> curated stack recipes
  GET  /api/categories            -> distinct categories
  GET  /api/image?url=...         -> proxied & disk-cached remote image (toggleable)
  POST /api/login/{provider}      -> (AUTH) open browser window for interactive store login
  POST /api/fetch/{provider}      -> (AUTH) refresh owned library using saved session
  POST /api/enrich                -> (AUTH) enrich N un-enriched assets (images/video links)
  POST /api/scan-local            -> (AUTH) rescan disk caches
  POST /api/import                -> (AUTH) unpack .unitypackage directly into project
"""
import hashlib
import os
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends, Header, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

try:
    from .db import init_db, search_assets, get_asset_by_id, get_stats, get_categories
    from .config import load_config, get_or_create_auth_token, evict_image_cache, __version__, WEB_DIR, is_safe_image_url, MAX_IMAGE_BYTES, VAULT_SOURCES
    from . import store_client, local_scan, unpacker, stack_rules, semantic
except ImportError:
    from db import init_db, search_assets, get_asset_by_id, get_stats, get_categories
    from config import load_config, get_or_create_auth_token, evict_image_cache, __version__, WEB_DIR, is_safe_image_url, MAX_IMAGE_BYTES, VAULT_SOURCES
    import store_client, local_scan, unpacker, stack_rules, semantic

init_db()

app = FastAPI(title="Quartermaster", docs_url="/api/docs")

# ----------------------------- CORS & Security -----------------------------
ALLOWED_ORIGINS = [
    "http://localhost:7890",
    "http://127.0.0.1:7890",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def check_cross_origin_defense(request: Request, call_next):
    # 1. Reject cross-origin browser fetch requests with unauthorized Origin
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
        return Response("Forbidden: Cross-Origin request blocked (CSRF/DoS protection)", status_code=403)

    # 2. Reject cross-origin subresource requests with unauthorized Referer
    referer = request.headers.get("referer")
    has_valid_referer = False
    if referer:
        import urllib.parse
        ref_parsed = urllib.parse.urlsplit(referer)
        ref_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}".rstrip("/")
        if ref_origin not in ALLOWED_ORIGINS:
            return Response("Forbidden: Invalid Referer origin (CSRF/DoS protection)", status_code=403)
        has_valid_referer = True

    # 3. For /api/ endpoints: if neither Origin nor Referer is present (e.g. <img referrerpolicy="no-referrer"> attack),
    # require a valid token (X-Quartermaster-Token, Authorization: Bearer, or session cookie)
    if request.url.path.startswith("/api/"):
        has_valid_origin = bool(origin and origin.rstrip("/") in ALLOWED_ORIGINS)
        if not has_valid_origin and not has_valid_referer:
            token = request.headers.get("X-Quartermaster-Token")
            if not token:
                auth = request.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    token = auth[7:].strip()
            if not token:
                token = request.cookies.get("quartermaster_token")

            expected = get_or_create_auth_token()
            if not token or token != expected:
                return Response("Forbidden: Authentication token or trusted local origin required (CSRF/DoS protection)", status_code=403)

    return await call_next(request)


def verify_auth(
    request: Request,
    x_qm_token: Optional[str] = Header(None, alias="X-Quartermaster-Token"),
    authorization: Optional[str] = Header(None),
    qm_cookie: Optional[str] = Cookie(None, alias="quartermaster_token")
):
    """
    Validates API authentication and enforces strict CSRF protections.
    Blocks simple-request form submissions and unauthorized cross-origin requests.
    """
    expected = get_or_create_auth_token()

    # 1. CSRF Defense: exact-match Origin / Referer if present
    import urllib.parse
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in ALLOWED_ORIGINS:
        raise HTTPException(403, "Forbidden: Cross-Origin request blocked (CSRF protection)")

    referer = request.headers.get("referer")
    if referer:
        ref_parsed = urllib.parse.urlsplit(referer)
        ref_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}".rstrip("/")
        if ref_origin not in ALLOWED_ORIGINS:
            raise HTTPException(403, "Forbidden: Invalid Referer origin (CSRF protection)")

    # 2. Token Check: X-Quartermaster-Token header OR Authorization: Bearer OR SameSite Cookie
    token = x_qm_token
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and qm_cookie:
        token = qm_cookie

    if not token or token != expected:
        raise HTTPException(401, "Unauthorized: Valid X-Quartermaster-Token or session cookie required.")


# ------------------------------- static UI ---------------------------------

@app.get("/")
def index():
    token = get_or_create_auth_token()
    response = FileResponse(os.path.join(WEB_DIR, "index.html"))
    # Set SameSite=Strict cookie for the web UI on localhost
    response.set_cookie(
        key="quartermaster_token",
        value=token,
        httponly=False,
        samesite="strict",
        path="/"
    )
    return response


# --------------------------------- Read API --------------------------------

@app.get("/api/assets")
def api_assets(query: str = "", category: str = "all", pipeline: str = "all",
               source: str = "all", local: str = "all", sort_by: str = "relevance",
               limit: int = 60, offset: int = 0):
    if query.strip():
        try:
            merged = semantic.hybrid_search(query.strip(), limit=max(limit * 3, 200))
            items = merged.get("results", [])
            filtered = []
            for item in items:
                if source != "all" and item.get("source") != source:
                    continue
                if category != "all" and item.get("category") != category:
                    continue
                if pipeline != "all":
                    pipes = item.get("render_pipelines") or item.get("formats") or []
                    if not any(pipeline.lower() in p.lower() for p in pipes):
                        continue
                if local != "all":
                    is_local = bool(item.get("local_path"))
                    if (local == "true" and not is_local) or (local == "false" and is_local):
                        continue
                filtered.append(item)

            if sort_by == "title_asc":
                filtered.sort(key=lambda x: (x.get("title") or "").lower())
            elif sort_by == "title_desc":
                filtered.sort(key=lambda x: (x.get("title") or "").lower(), reverse=True)
            elif sort_by == "claimed_desc":
                filtered.sort(key=lambda x: x.get("claimed_at") or "", reverse=True)
            elif sort_by == "size_desc":
                filtered.sort(key=lambda x: x.get("size_bytes") or 0, reverse=True)
            # sort_by == "relevance" keeps hybrid_search ranking

            paged = filtered[offset : offset + limit]
            return {
                "items": paged,
                "stats": get_stats(),
                "total": len(filtered),
                "search_mode": merged.get("search_mode", "3-way-hybrid"),
            }
        except Exception:
            pass

    # Empty query or fallback
    db_sort = sort_by if sort_by != "relevance" else "title_asc"
    items = search_assets(query=query or None, category=category, pipeline=pipeline,
                          source=source, local=None if local == "all" else local,
                          sort_by=db_sort, limit=min(limit, 2000), offset=offset)
    return {
        "items": items,
        "stats": get_stats(),
    }


@app.get("/api/asset/{asset_id}")
def api_asset(asset_id: str):
    item = get_asset_by_id(asset_id)
    if not item:
        raise HTTPException(404, "Asset not found")
    return item


@app.get("/api/stats")
def api_stats():
    st = get_stats()
    st["version"] = __version__
    return st


@app.get("/api/recipes")
def api_recipes():
    try:
        return {"recipes": stack_rules.list_recipes()}
    except Exception as e:
        print(f"[warn] /api/recipes error: {e}")
        return {"recipes": []}


@app.get("/api/categories")
def api_categories():
    return {"categories": get_categories()}


# --------------------------- media proxy cache (SSRF Protected) -----------------------------

@app.get("/api/image")
def api_image(url: str):
    """
    Proxies and caches cover/gallery images from allowlisted CDN domains.
    Guards against SSRF via domain allowlisting, loopback/metadata IP blocking,
    redirect re-validation, and response size limits.
    """
    if not is_safe_image_url(url):
        raise HTTPException(400, "Forbidden: URL is not an allowlisted CDN domain or points to an unsafe host.")

    cfg = load_config()
    cache_dir = cfg["media_cache_dir"]
    key = hashlib.sha1(url.encode()).hexdigest()
    ext = ".jpg"
    for e in (".png", ".webp", ".gif", ".jpeg"):
        if e in url.lower():
            ext = e
            break
    cached = os.path.join(cache_dir, key + ext)

    if cfg["media_cache_enabled"] and os.path.exists(cached):
        with open(cached, "rb") as f:
            data = f.read()
        return Response(data, media_type=f"image/{ext.lstrip('.')}")

    import httpx
    try:
        # Step-by-step redirect validation with proper connection cleanup
        current_url = url
        with httpx.Client(timeout=15.0, headers={"User-Agent": "Mozilla/5.0", "Referer": url}) as client:
            for _ in range(5):  # max 5 redirects
                if not is_safe_image_url(current_url):
                    raise HTTPException(400, "Forbidden: Redirect target failed security allowlist check.")

                req = client.build_request("GET", current_url)
                r = client.send(req, stream=True)

                if r.is_redirect:
                    loc = r.headers.get("location")
                    if not loc:
                        break
                    import urllib.parse
                    current_url = urllib.parse.urljoin(current_url, loc)
                    r.close()
                    continue

                r.raise_for_status()
                ctype = r.headers.get("content-type", "").lower()
                if not any(t in ctype for t in ("image/", "application/octet-stream", "binary/octet-stream")):
                    r.close()
                    raise HTTPException(400, f"Invalid content-type: {ctype}. Only image resources are proxied.")

                # Stream with size cap
                chunks = []
                total_bytes = 0
                for chunk in r.iter_bytes(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > MAX_IMAGE_BYTES:
                        r.close()
                        raise HTTPException(413, "Image exceeds maximum allowed size (15MB).")
                    chunks.append(chunk)
                r.close()
                content = b"".join(chunks)

                if cfg["media_cache_enabled"]:
                    os.makedirs(cache_dir, exist_ok=True)
                    evict_image_cache(cache_dir)
                    with open(cached, "wb") as f:
                        f.write(content)

                return Response(content, media_type=ctype if "image/" in ctype else "image/jpeg")

        raise HTTPException(502, "Too many redirects while proxying image.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Proxy fetch failed: {e}")


# -------------------- Protected State-Mutating APIs ------------------------

@app.post("/api/login/{provider}", dependencies=[Depends(verify_auth)])
def api_login(provider: str):
    valid_providers = tuple(p for p in VAULT_SOURCES if p != "quixel")
    if provider not in valid_providers:
        raise HTTPException(400, f"provider must be one of: {', '.join(valid_providers)}")

    def run():
        ok = store_client.interactive_login(provider)
        print(f"[login] {provider}: {'success' if ok else 'failed'}")

    threading.Thread(target=run, daemon=True).start()
    return {"status": "browser-opening",
            "message": f"A browser window is opening. Sign in to {provider}; the session is saved automatically when done."}


@app.post("/api/fetch/{provider}", dependencies=[Depends(verify_auth)])
def api_fetch(provider: str):
    valid_providers = tuple(p for p in VAULT_SOURCES if p != "quixel")
    if provider not in valid_providers:
        raise HTTPException(400, f"provider must be one of: {', '.join(valid_providers)}")
    if not store_client.has_saved_session(provider):
        raise HTTPException(409, {"error": f"No saved login for {provider}. Call /api/login/{provider} first."})
    count = store_client.fetch_library(provider)
    return {"status": "ok", "provider": provider, "assets_seen": count}


@app.post("/api/enrich", dependencies=[Depends(verify_auth)])
def api_enrich(limit: Optional[int] = None):
    count = store_client.enrich_assets(limit)
    return {"status": "ok", "enriched": count}


@app.post("/api/scan-local", dependencies=[Depends(verify_auth)])
def api_scan_local():
    """Scan Unity/Fab disk caches and tag assets as locally downloaded."""
    return local_scan.scan_all()


@app.post("/api/import", dependencies=[Depends(verify_auth)])
async def api_import(request: Request):
    """Unpack a locally-downloaded .unitypackage into a Unity project root.
    Protected against CSRF via X-VaultMCP-Token auth header."""
    form = dict(await request.form())
    if not form:
        form = dict(request.query_params)
    asset_id = form.get("asset_id", "")
    project_dir = form.get("project_dir", "")
    strip_demos = str(form.get("strip_demos", "true")).lower() != "false"
    if not asset_id or not project_dir:
        raise HTTPException(400, "asset_id and project_dir are required")
    try:
        result = unpacker.import_asset_to_project(asset_id, project_dir,
                                                  strip_demos=strip_demos)
        # list prefabs so the editor can offer 'add to scene'
        prefab_hits = []
        title_words = [w for w in __import__("re").findall(
            r"[a-zA-Z]+", result.get("title", "")) if len(w) > 3][:4]
        for root, _dirs, files in os.walk(os.path.join(project_dir, "Assets")):
            for f in files:
                if f.endswith(".prefab") and any(w.lower() in f.lower() for w in title_words):
                    prefab_hits.append(os.path.join(root, f))
                if len(prefab_hits) >= 10:
                    break
            if len(prefab_hits) >= 10:
                break
        result["prefabs"] = prefab_hits
        return result
    except Exception as e:
        raise HTTPException(400, str(e))


# serve web assets (css/js)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    tok = get_or_create_auth_token()
    print(f"Quartermaster UI -> http://localhost:{cfg['server_port']}")
    print(f"API Auth Token active ({len(tok)} chars)")
    uvicorn.run(app, host="127.0.0.1", port=cfg["server_port"], log_level="warning")
