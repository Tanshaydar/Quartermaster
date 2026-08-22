"""
VaultMCP local web server & REST API.

  python -m src.server            # serves on http://localhost:7890

Endpoints:
  GET  /                          -> dark-mode search UI
  GET  /api/assets                -> search/filter (query, category, pipeline, source, limit, offset)
  GET  /api/asset/{id}            -> full details
  GET  /api/stats                 -> counts by category/source
  GET  /api/categories            -> distinct categories
  GET  /api/image?url=...         -> proxied & disk-cached remote image (toggleable)
  POST /api/login/{provider}      -> open browser window for interactive store login
  POST /api/fetch/{provider}      -> refresh owned library using saved session
  POST /api/enrich                -> enrich N un-enriched assets (images/video links)
"""
import hashlib
import os
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

try:
    from .db import search_assets, get_asset_by_id, get_stats, get_categories
    from .config import load_config
    from . import store_client, local_scan, unpacker
except ImportError:
    from db import search_assets, get_asset_by_id, get_stats, get_categories
    from config import load_config
    import store_client, local_scan, unpacker

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT_DIR, "web")

app = FastAPI(title="VaultMCP", docs_url="/api/docs")


# ------------------------------- static UI ---------------------------------

@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


# --------------------------------- API -------------------------------------

@app.get("/api/assets")
def api_assets(query: str = "", category: str = "all", pipeline: str = "all",
               source: str = "all", limit: int = 60, offset: int = 0):
    return {
        "items": search_assets(query=query or None, category=category, pipeline=pipeline,
                               source=source, limit=min(limit, 500), offset=offset),
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
    return get_stats()


@app.get("/api/categories")
def api_categories():
    return {"categories": get_categories()}


# --------------------------- media proxy cache -----------------------------

@app.get("/api/image")
def api_image(url: str):
    cfg = load_config()
    if not url.startswith("http"):
        raise HTTPException(400, "Invalid URL")

    cache_dir = cfg["media_cache_dir"]
    key = hashlib.sha1(url.encode()).hexdigest()
    ext = ".jpg"
    for e in (".png", ".webp", ".gif"):
        if e in url.lower():
            ext = e
            break
    cached = os.path.join(cache_dir, key + ext)

    if cfg["media_cache_enabled"] and os.path.exists(cached):
        with open(cached, "rb") as f:
            data = f.read()
        return Response(data, media_type=f"image{ext}")

    import httpx
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": url})
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"Fetch failed: {e}")

    if cfg["media_cache_enabled"]:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cached, "wb") as f:
            f.write(r.content)

    ctype = r.headers.get("content-type", "image/jpeg")
    return Response(r.content, media_type=ctype)


# ------------------------ login / fetch / enrich ---------------------------

@app.post("/api/login/{provider}")
def api_login(provider: str):
    if provider not in ("unity", "fab"):
        raise HTTPException(400, "provider must be 'unity' or 'fab'")

    def run():
        ok = store_client.interactive_login(provider)
        print(f"[login] {provider}: {'success' if ok else 'failed'}")

    threading.Thread(target=run, daemon=True).start()
    return {"status": "browser-opening",
            "message": f"A browser window is opening. Sign in to {provider}; the session is saved automatically when done."}


@app.post("/api/fetch/{provider}")
def api_fetch(provider: str):
    if provider not in ("unity", "fab"):
        raise HTTPException(400, "provider must be 'unity' or 'fab'")
    if not store_client.has_saved_session(provider):
        raise HTTPException(409, {"error": f"No saved login for {provider}. Call /api/login/{provider} first."})
    count = store_client.fetch_library(provider)
    return {"status": "ok", "provider": provider, "assets_seen": count}


@app.post("/api/enrich")
def api_enrich(limit: Optional[int] = None):
    count = store_client.enrich_assets(limit)
    return {"status": "ok", "enriched": count}


@app.post("/api/scan-local")
def api_scan_local():
    """Scan Unity/Fab disk caches and tag assets as locally downloaded."""
    return local_scan.scan_all()


@app.post("/api/import")
async def api_import(request: Request):
    """Unpack a locally-downloaded .unitypackage into a Unity project root.
    Used by the in-editor VaultMCP window (project_dir = the Unity project).
    Accepts application/x-www-form-urlencoded or query params."""
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
    print(f"VaultMCP UI -> http://localhost:{cfg['server_port']}")
    uvicorn.run(app, host="127.0.0.1", port=cfg["server_port"], log_level="warning")
