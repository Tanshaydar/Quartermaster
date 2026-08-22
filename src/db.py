import sqlite3
import json
import os
from typing import List, Dict, Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "assets.db")

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    # WAL mode: concurrent readers (MCP agent) never block the writer (GUI)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        package_id TEXT,
        product_id TEXT,
        title TEXT NOT NULL,
        publisher TEXT,
        publisher_id TEXT,
        version TEXT,
        size_mb REAL,
        size_str TEXT,
        claimed_date TEXT,
        store_url TEXT,
        category TEXT,
        render_pipelines TEXT,
        tags TEXT,
        summary TEXT,
        usage_notes TEXT,
        image_url TEXT,
        gallery_images TEXT,
        video_links TEXT,
        formats TEXT,
        license TEXT,
        enriched INTEGER DEFAULT 0,
        enriched_at TIMESTAMP,
        local_path TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cur.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
        id UNINDEXED,
        title,
        publisher,
        category,
        tags,
        summary,
        usage_notes,
        render_pipelines,
        tokenize = 'unicode61 remove_diacritics 2'
    );
    """)

    conn.commit()
    conn.close()

def upsert_asset(asset: Dict[str, Any], db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()

    render_pipelines = json.dumps(asset.get("render_pipelines", [])) if isinstance(asset.get("render_pipelines"), list) else asset.get("render_pipelines", "[]")
    tags = json.dumps(asset.get("tags", [])) if isinstance(asset.get("tags"), list) else asset.get("tags", "[]")
    gallery_images = json.dumps(asset.get("gallery_images", [])) if isinstance(asset.get("gallery_images"), list) else asset.get("gallery_images", "[]")
    video_links = json.dumps(asset.get("video_links", [])) if isinstance(asset.get("video_links"), list) else asset.get("video_links", "[]")
    formats = json.dumps(asset.get("formats", [])) if isinstance(asset.get("formats"), list) else asset.get("formats", "")
    enriched = 1 if asset.get("enriched") else 0

    cur.execute("""
    INSERT INTO assets (
        id, source, package_id, product_id, title, publisher, publisher_id,
        version, size_mb, size_str, claimed_date, store_url, category,
        render_pipelines, tags, summary, usage_notes, image_url, gallery_images,
        video_links, formats, license, enriched, local_path
    ) VALUES (
        :id, :source, :package_id, :product_id, :title, :publisher, :publisher_id,
        :version, :size_mb, :size_str, :claimed_date, :store_url, :category,
        :render_pipelines, :tags, :summary, :usage_notes, :image_url, :gallery_images,
        :video_links, :formats, :license, :enriched, :local_path
    )
    ON CONFLICT(id) DO UPDATE SET
        title=excluded.title,
        publisher=excluded.publisher,
        version=excluded.version,
        size_mb=excluded.size_mb,
        size_str=excluded.size_str,
        claimed_date=excluded.claimed_date,
        store_url=excluded.store_url,
        category=excluded.category,
        render_pipelines=excluded.render_pipelines,
        tags=excluded.tags,
        summary=excluded.summary,
        usage_notes=excluded.usage_notes,
        image_url=CASE WHEN excluded.image_url != '' THEN excluded.image_url ELSE assets.image_url END,
        gallery_images=CASE WHEN excluded.gallery_images != '[]' THEN excluded.gallery_images ELSE assets.gallery_images END,
        video_links=excluded.video_links,
        formats=excluded.formats,
        license=excluded.license,
        enriched=MAX(assets.enriched, excluded.enriched),
        local_path=CASE WHEN excluded.local_path != '' THEN excluded.local_path ELSE assets.local_path END;
    """, {
        "id": asset["id"],
        "source": asset.get("source", "unity"),
        "package_id": asset.get("package_id", ""),
        "product_id": asset.get("product_id", ""),
        "title": asset["title"],
        "publisher": asset.get("publisher", ""),
        "publisher_id": asset.get("publisher_id", ""),
        "version": asset.get("version", ""),
        "size_mb": asset.get("size_mb", 0.0),
        "size_str": asset.get("size_str", ""),
        "claimed_date": asset.get("claimed_date", ""),
        "store_url": asset.get("store_url", ""),
        "category": asset.get("category", "General"),
        "render_pipelines": render_pipelines,
        "tags": tags,
        "summary": asset.get("summary", ""),
        "usage_notes": asset.get("usage_notes", ""),
        "image_url": asset.get("image_url", ""),
        "gallery_images": gallery_images,
        "video_links": video_links,
        "formats": formats,
        "license": asset.get("license", ""),
        "enriched": enriched,
        "local_path": asset.get("local_path", "")
    })

    cur.execute("DELETE FROM assets_fts WHERE id = ?", (asset["id"],))
    cur.execute("""
    INSERT INTO assets_fts (id, title, publisher, category, tags, summary, usage_notes, render_pipelines)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        asset["id"],
        asset["title"],
        asset.get("publisher", ""),
        asset.get("category", ""),
        tags,
        asset.get("summary", ""),
        asset.get("usage_notes", ""),
        render_pipelines
    ))

    conn.commit()
    conn.close()

def search_assets(
    query: Optional[str] = None,
    category: Optional[str] = None,
    pipeline: Optional[str] = None,
    source: Optional[str] = None,
    local: Optional[str] = None,   # 'local' | 'cloud' | None
    limit: int = 100,
    offset: int = 0,
    db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    cur = conn.cursor()

    params = []
    if query and query.strip():
        clean_q = query.replace('"', '""').strip()
        terms = [f'"{t}"*' for t in clean_q.split() if t]
        fts_expr = " AND ".join(terms)
        sql = """
        SELECT a.*, fts.rank
        FROM assets_fts fts
        JOIN assets a ON a.id = fts.id
        WHERE assets_fts MATCH ?
        """
        params.append(fts_expr)
    else:
        sql = "SELECT a.*, 0 as rank FROM assets a WHERE 1=1"

    if category and category.lower() != "all":
        sql += " AND a.category = ?"
        params.append(category)

    if pipeline and pipeline.lower() != "all":
        sql += " AND a.render_pipelines LIKE ?"
        params.append(f"%{pipeline}%")

    if source and source.lower() != "all":
        sql += " AND a.source = ?"
        params.append(source)

    if local == "local":
        sql += " AND a.local_path != ''"
    elif local == "cloud":
        sql += " AND a.local_path = ''"

    if query and query.strip():
        sql += " ORDER BY fts.rank ASC LIMIT ? OFFSET ?"
    else:
        sql += " ORDER BY a.claimed_date DESC, a.title ASC LIMIT ? OFFSET ?"

    params.extend([limit, offset])

    cur.execute(sql, params)
    rows = cur.fetchall()

    results = []
    for r in rows:
        item = dict(r)
        for k in ["render_pipelines", "tags", "gallery_images", "video_links", "formats"]:
            try:
                item[k] = json.loads(item.get(k) or "[]")
            except:
                item[k] = []
        results.append(item)

    conn.close()
    return results

def get_asset_by_id(asset_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM assets WHERE id = ? OR package_id = ?", (asset_id, asset_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    for key in ["render_pipelines", "tags", "gallery_images", "video_links", "formats"]:
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except:
            item[key] = []
    return item

def get_categories(db_path: str = DB_PATH) -> List[str]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT category FROM assets ORDER BY category")
    cats = [r["category"] for r in cur.fetchall()]
    conn.close()
    return cats

def get_unenriched(limit: int = 50, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM assets WHERE enriched = 0 ORDER BY RANDOM() LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def mark_enriched(asset_id: str, image_url: str = "", gallery_images=None, video_links=None,
                  summary: str = "", usage_notes: str = "", db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()
    sets, params = ["enriched = 1", "enriched_at = CURRENT_TIMESTAMP"], []
    if image_url:
        sets.append("image_url = ?"); params.append(image_url)
    if gallery_images:
        sets.append("gallery_images = ?"); params.append(json.dumps(gallery_images))
    if video_links is not None:
        sets.append("video_links = ?"); params.append(json.dumps(video_links))
    if summary:
        sets.append("summary = ?"); params.append(summary)
    if usage_notes:
        sets.append("usage_notes = ?"); params.append(usage_notes)
    params.append(asset_id)
    cur.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id = ?", params)

    if image_url or gallery_images or summary or usage_notes:
        cur.execute("SELECT title, publisher, category, tags, summary, usage_notes, render_pipelines FROM assets WHERE id = ?", (asset_id,))
        row = cur.fetchone()
        if row:
            cur.execute("DELETE FROM assets_fts WHERE id = ?", (asset_id,))
            cur.execute("INSERT INTO assets_fts (id, title, publisher, category, tags, summary, usage_notes, render_pipelines) VALUES (?,?,?,?,?,?,?,?)",
                        (asset_id, row["title"], row["publisher"], row["category"], row["tags"], row["summary"], row["usage_notes"], row["render_pipelines"]))
    conn.commit()
    conn.close()

def get_stats(db_path: str = DB_PATH) -> Dict[str, Any]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total FROM assets")
    total = cur.fetchone()["total"]

    cur.execute("SELECT category, COUNT(*) as count FROM assets GROUP BY category ORDER BY count DESC")
    categories = {r["category"]: r["count"] for r in cur.fetchall()}

    cur.execute("SELECT source, COUNT(*) as count FROM assets GROUP BY source")
    sources = {r["source"]: r["count"] for r in cur.fetchall()}

    cur.execute("SELECT COUNT(*) as c FROM assets WHERE local_path != ''")
    downloaded = cur.fetchone()["c"]

    conn.close()
    return {"total": total, "categories": categories,
            "sources": sources, "downloaded_locally": downloaded}
