import sqlite3
import json
import os
from typing import List, Dict, Any, Optional

try:
    from .config import DB_PATH
except ImportError:
    from config import DB_PATH

import threading

_DB_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    global _DB_INITIALIZED
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if not _DB_INITIALIZED and db_path == DB_PATH:
        with _DB_INIT_LOCK:
            if not _DB_INITIALIZED:
                init_db(db_path)
                _DB_INITIALIZED = True
    conn = sqlite3.connect(db_path, timeout=10)
    # WAL mode: concurrent readers (MCP agent) never block the writer (GUI)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
        vision_tags TEXT DEFAULT '',
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
        vision_tags,
        tokenize = 'unicode61 remove_diacritics 2'
    );
    """)

    # Schema & FTS data migration: user_version 3 ensures FTS includes vision_tags and is fully rebuilt
    ver = cur.execute("PRAGMA user_version").fetchone()[0]
    if ver < 3:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(assets)")]
        if "vision_tags" not in cols:
            cur.execute("ALTER TABLE assets ADD COLUMN vision_tags TEXT DEFAULT ''")

        has_assets = cur.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        cur.execute("DROP TABLE IF EXISTS assets_fts")
        cur.execute("""
            CREATE VIRTUAL TABLE assets_fts USING fts5(
                id UNINDEXED,
                title,
                publisher,
                category,
                tags,
                summary,
                usage_notes,
                render_pipelines,
                vision_tags,
                tokenize = 'unicode61 remove_diacritics 2'
            );
        """)
        if has_assets > 0:
            cur.execute("""
                INSERT INTO assets_fts (id, title, publisher, category, tags, summary, usage_notes, render_pipelines, vision_tags)
                SELECT id, title, publisher, category, tags, summary, usage_notes, render_pipelines, COALESCE(vision_tags, '') FROM assets
            """)
        cur.execute("PRAGMA user_version = 3")

    # Prune any dangling FTS entries whose parent row in assets was removed
    try:
        cur.execute("DELETE FROM assets_fts WHERE id NOT IN (SELECT id FROM assets)")
    except Exception:
        pass

    # Prune any dangling image_vectors whose referenced assets were removed.
    # asset_id holds a ';'-joined list when several assets share one image, so a
    # plain NOT IN would delete every shared-cover row. Keep a row if ANY of its
    # referenced assets survives -- same rule as vision._prune in src/vision.py.
    try:
        cur.execute(
            "DELETE FROM image_vectors "
            "WHERE asset_id NOT LIKE '%;%' AND asset_id NOT IN (SELECT id FROM assets)"
        )
        joined = cur.execute(
            "SELECT id, asset_id FROM image_vectors WHERE asset_id LIKE '%;%'"
        ).fetchall()
        if joined:
            alive = {r[0] for r in cur.execute("SELECT id FROM assets")}
            for uid, aid in joined:
                refs = {a.strip() for a in (aid or "").split(";") if a.strip()}
                if not refs or not (refs & alive):
                    cur.execute("DELETE FROM image_vectors WHERE id = ?", (uid,))
    except Exception:
        pass

    conn.commit()
    conn.close()

def _sync_fts(cur: sqlite3.Cursor, asset_id: str):
    """Centralized FTS sync to ensure assets_fts never drifts."""
    cur.execute("DELETE FROM assets_fts WHERE id = ?", (asset_id,))
    cur.execute("""
        INSERT INTO assets_fts (id, title, publisher, category, tags, summary, usage_notes, render_pipelines, vision_tags)
        SELECT id, title, publisher, category, tags, summary, usage_notes, render_pipelines, COALESCE(vision_tags, '')
        FROM assets WHERE id = ?
    """, (asset_id,))


def upsert_asset(asset: Dict[str, Any], db_path: str = DB_PATH):
    conn = get_connection(db_path)
    cur = conn.cursor()

    render_pipelines = json.dumps(asset.get("render_pipelines", [])) if isinstance(asset.get("render_pipelines"), list) else asset.get("render_pipelines", "[]")
    tags = json.dumps(asset.get("tags", [])) if isinstance(asset.get("tags"), list) else asset.get("tags", "[]")
    gallery_images = json.dumps(asset.get("gallery_images", [])) if isinstance(asset.get("gallery_images"), list) else asset.get("gallery_images", "[]")
    video_links = json.dumps(asset.get("video_links", [])) if isinstance(asset.get("video_links"), list) else asset.get("video_links", "[]")
    formats = json.dumps(asset.get("formats", [])) if isinstance(asset.get("formats"), list) else asset.get("formats", "")
    vision_tags = json.dumps(asset.get("vision_tags", [])) if isinstance(asset.get("vision_tags"), list) else (asset.get("vision_tags") or "[]")
    enriched = 1 if asset.get("enriched") else 0

    cur.execute("""
    INSERT INTO assets (
        id, source, package_id, product_id, title, publisher, publisher_id,
        version, size_mb, size_str, claimed_date, store_url, category,
        render_pipelines, tags, summary, usage_notes, image_url, gallery_images,
        video_links, formats, license, enriched, local_path, vision_tags
    ) VALUES (
        :id, :source, :package_id, :product_id, :title, :publisher, :publisher_id,
        :version, :size_mb, :size_str, :claimed_date, :store_url, :category,
        :render_pipelines, :tags, :summary, :usage_notes, :image_url, :gallery_images,
        :video_links, :formats, :license, :enriched, :local_path, :vision_tags
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
        vision_tags=CASE WHEN excluded.vision_tags != '[]' AND excluded.vision_tags != '' THEN excluded.vision_tags ELSE assets.vision_tags END,
        summary=CASE WHEN assets.enriched = 1 AND excluded.enriched = 0 THEN assets.summary ELSE (CASE WHEN excluded.summary != '' THEN excluded.summary ELSE assets.summary END) END,
        usage_notes=CASE WHEN assets.enriched = 1 AND excluded.enriched = 0 THEN assets.usage_notes ELSE (CASE WHEN excluded.usage_notes != '' THEN excluded.usage_notes ELSE assets.usage_notes END) END,
        image_url=CASE WHEN excluded.image_url != '' THEN excluded.image_url ELSE assets.image_url END,
        gallery_images=CASE WHEN excluded.gallery_images != '[]' THEN excluded.gallery_images ELSE assets.gallery_images END,
        video_links=CASE WHEN assets.enriched = 1 AND excluded.enriched = 0 THEN assets.video_links ELSE (CASE WHEN excluded.video_links != '[]' THEN excluded.video_links ELSE assets.video_links END) END,
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
        "local_path": asset.get("local_path", ""),
        "vision_tags": vision_tags,
    })

    _sync_fts(cur, asset["id"])
    conn.commit()
    conn.close()

def search_assets(
    query: Optional[str] = None,
    category: Optional[str] = None,
    pipeline: Optional[str] = None,
    source: Optional[str] = None,
    local: Optional[str] = None,   # 'local' | 'cloud' | None
    sort_by: Optional[str] = "title_asc",
    limit: int = 100,
    offset: int = 0,
    db_path: str = DB_PATH
) -> List[Dict[str, Any]]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    try:
        try:
            cur.execute("SELECT 1 FROM assets LIMIT 1")
        except sqlite3.OperationalError:
            conn.close()
            init_db(db_path)
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
            if sort_by == "title_desc":
                sql += " ORDER BY a.title COLLATE NOCASE DESC LIMIT ? OFFSET ?"
            elif sort_by == "claimed_desc":
                sql += " ORDER BY a.claimed_date DESC, a.title COLLATE NOCASE ASC LIMIT ? OFFSET ?"
            elif sort_by == "size_desc":
                sql += " ORDER BY a.size_mb DESC, a.title COLLATE NOCASE ASC LIMIT ? OFFSET ?"
            else:
                sql += " ORDER BY fts.rank ASC, a.title COLLATE NOCASE ASC LIMIT ? OFFSET ?"
        else:
            if sort_by == "title_desc":
                sql += " ORDER BY a.title COLLATE NOCASE DESC LIMIT ? OFFSET ?"
            elif sort_by == "claimed_desc":
                sql += " ORDER BY a.claimed_date DESC, a.title COLLATE NOCASE ASC LIMIT ? OFFSET ?"
            elif sort_by == "size_desc":
                sql += " ORDER BY a.size_mb DESC, a.title COLLATE NOCASE ASC LIMIT ? OFFSET ?"
            else:
                sql += " ORDER BY a.title COLLATE NOCASE ASC LIMIT ? OFFSET ?"

        params.extend([limit, offset])

        cur.execute(sql, params)
        rows = cur.fetchall()

        results = []
        for r in rows:
            item = dict(r)
            for k in ["render_pipelines", "tags", "gallery_images", "video_links", "formats", "vision_tags"]:
                if item.get(k) and isinstance(item[k], str):
                    try:
                        item[k] = json.loads(item[k])
                    except Exception:
                        item[k] = []
                elif not item.get(k):
                    item[k] = []
            results.append(item)
        return results
    except Exception:
        return []
    finally:
        conn.close()

def get_asset_by_id(asset_id: str, db_path: str = DB_PATH) -> Optional[Dict[str, Any]]:
    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM assets WHERE id = ? OR package_id = ?", (asset_id, asset_id))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    item = dict(row)
    for key in ["render_pipelines", "tags", "gallery_images", "video_links", "formats", "vision_tags"]:
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

    _sync_fts(cur, asset_id)
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
            "sources": sources, "by_source": sources, "downloaded_locally": downloaded}


def reclassify_all_assets(db_path: str = DB_PATH) -> int:
    """Reclassifies all assets using multimodal (vision + tags + title + summary) taxonomy and resyncs FTS5."""
    try:
        from .ingest import classify_asset
    except ImportError:
        from ingest import classify_asset

    conn = get_connection(db_path)
    cur = conn.cursor()
    rows = cur.execute("SELECT id, title, publisher, tags, vision_tags, summary, category FROM assets").fetchall()
    updated = 0
    for r in rows:
        tags = []
        if r["tags"]:
            try:
                tags = json.loads(r["tags"]) if isinstance(r["tags"], str) and r["tags"].startswith("[") else [r["tags"]]
            except Exception:
                tags = []
        info = classify_asset(
            title=r["title"],
            publisher=r["publisher"] or "",
            tags_list=tags,
            vision_tags=r["vision_tags"] or "",
            summary_text=r["summary"] or ""
        )
        new_cat = info["category"]
        if new_cat != r["category"]:
            cur.execute("UPDATE assets SET category = ? WHERE id = ?", (new_cat, r["id"]))
            _sync_fts(cur, r["id"])
            updated += 1
    conn.commit()
    conn.close()
    return updated
