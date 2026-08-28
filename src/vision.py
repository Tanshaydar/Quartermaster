"""
Vision pass: CLIP image embeddings + zero-shot concept mining for galleries.

Downloads each unique gallery/cover image once, encodes it with fastembed's
ONNX CLIP vision model, stores per-image vectors, then mines user-editable
visual concepts (data/concepts.json) against the whole corpus. Concept tags
that clear the corpus-calibrated threshold are written to the asset's
vision_tags column — a separate column so fetch/enrichment can never clobber
them.

Design decisions (from the architecture review):
  - Vectors are per-IMAGE, not per-asset: "gothic church in shot 7" stays
    attributable; the table is still tiny (~25MB at vault scale).
  - Thresholds self-calibrate: every build scores the ENTIRE stored corpus
    against all concepts and computes per-concept mean/std, so no magic
    numbers live in code or config.
  - Re-runs resume: URLs already present in image_vectors are skipped.
  - Plain HTTP works for both CDNs (media.fab.com and Unity's CDN serve
    images publicly) — no browser session needed for this pass.
  - Additive only: creates its own table/column; upsert_asset never touches
    vision_tags, so nothing existing changes behavior.

CLI:
  python -m src.vision build [--limit N]   # incremental encode + tag
  python -m src.vision status              # coverage report

Config keys (optional):
  vision_batch_size   (default 8)     images per ONNX batch
  vision_concurrency  (default 6)     parallel downloads
"""
import functools
import io
import json
import os
import sys
import hashlib

import numpy as np

try:
    from .db import get_connection, DB_PATH
    from .config import load_config, CONCEPTS_PATH, is_safe_image_url, MAX_IMAGE_BYTES
except ImportError:
    from db import get_connection, DB_PATH
    from config import load_config, CONCEPTS_PATH, is_safe_image_url, MAX_IMAGE_BYTES

VISION_MODEL = "Qdrant/clip-ViT-B-32-vision"
CLIP_TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"


# ---------------------------------------------------------------- schema ----

def _ensure_schema(conn):
    """Create the vision tables/columns this module owns. Idempotent."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS image_vectors (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            image_url TEXT NOT NULL,
            vector BLOB NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_img_vec_asset ON image_vectors(asset_id)")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(assets)")]
    if "vision_tags" not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN vision_tags TEXT DEFAULT ''")
    conn.commit()


# ------------------------------------------------------------- work list ----

def _collect_work(max_per_asset: int = 4):
    """Map unique gallery/cover URLs (cover + top hero screenshots) -> asset ids."""
    work = {}
    conn = get_connection()
    for aid, cover, gjson in conn.execute(
            "SELECT id, image_url, gallery_images FROM assets "
            "WHERE source IN ('unity','fab','quixel')"):
        urls = []
        if cover:
            urls.append(cover)
        try:
            gallery = json.loads(gjson or "[]")
            for g in gallery:
                if g and g != cover and g not in urls:
                    urls.append(g)
        except Exception:
            pass
        for u in urls[:max_per_asset]:
            if isinstance(u, str) and u.startswith("http"):
                work.setdefault(u, set()).add(aid)
    conn.close()
    return work


def _norm_url(u: str) -> str:
    return "https:" + u if u.startswith("//") else u


# ------------------------------------------------------------ downloads -----

def _download_worker(client, url: str, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        return None
    normalized = _norm_url(url)
    if not is_safe_image_url(normalized):
        return None
    try:
        r = client.get(normalized)
        if r.status_code != 200 or len(r.content) > MAX_IMAGE_BYTES:
            return None
        return r.content
    except Exception:
        return None


# ------------------------------------------------------- concept tagging ----

def _load_concepts():
    if not os.path.exists(CONCEPTS_PATH):
        return [], 2.0
    try:
        with open(CONCEPTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        concepts = [c.strip() for c in data.get("concepts", []) if c.strip()]
        threshold_z = float(data.get("threshold_z", 2.0))
        return concepts, threshold_z
    except Exception as e:
        print(f"[warn] Failed to load visual concepts from {CONCEPTS_PATH}: {e}", file=sys.stderr)
        return [], 2.0


def _mine_concepts(all_vecs, asset_map, concepts, threshold_z=2.2, min_cosine=0.24, max_tags_per_asset=3, text_model=None):
    """Score every stored vector against concepts using dual cosine & Z-score calibration.
    
    Filters out background statistical noise by requiring:
      1. Absolute CLIP cosine similarity >= min_cosine (0.24)
      2. Relative outlier Z-score >= threshold_z (2.2)
      3. Capped to at most `max_tags_per_asset` (3) highest-scoring concepts per asset.
    """
    from fastembed import TextEmbedding
    if text_model is None:
        text_model = TextEmbedding(CLIP_TEXT_MODEL)

    C = np.array([np.array(list(text_model.embed([c]))[0], dtype=np.float32)
                  for c in concepts])
    C /= (np.linalg.norm(C, axis=1, keepdims=True) + 1e-9)

    urls = list(all_vecs.keys())
    V = np.array([all_vecs[u] for u in urls], dtype=np.float32)
    S = V @ C.T                                   # (N images, K concepts)

    N = S.shape[0]
    if N < 50:
        print(f"[vision] only {N} vectors indexed — skipping tagging until "
              f"corpus is larger (calibration needs volume)")
        return {}

    # Calculate column-wise Z-scores
    Z = np.zeros_like(S)
    for k in range(S.shape[1]):
        col = S[:, k]
        mu, sigma = float(col.mean()), float(col.std())
        if sigma > 1e-6:
            Z[:, k] = (col - mu) / sigma

    asset_concept_scores = {}
    for row_idx, url in enumerate(urls):
        for aid in asset_map.get(url, ()):
            for k, concept in enumerate(concepts):
                cos_val = S[row_idx, k]
                z_val = Z[row_idx, k]
                if cos_val >= min_cosine and z_val >= threshold_z:
                    score = cos_val * z_val
                    curr = asset_concept_scores.setdefault(aid, {}).get(concept, 0)
                    if score > curr:
                        asset_concept_scores[aid][concept] = score

    # Pick top K concepts per asset
    final_tags = {}
    for aid, c_scores in asset_concept_scores.items():
        sorted_concepts = sorted(c_scores.items(), key=lambda x: -x[1])[:max_tags_per_asset]
        final_tags[aid] = sorted([c for c, _ in sorted_concepts])

    return final_tags


# ------------------------------------------------------------------ build ---

def build(limit=None, cancel_event=None, progress=None) -> dict:
    """Incremental: download+encode missing images, then re-mine all tags."""
    import httpx
    from PIL import Image
    from fastembed import ImageEmbedding
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cfg = load_config()
    batch_size = max(1, int(cfg.get("vision_batch_size", 16)))
    concurrency = max(1, int(cfg.get("vision_concurrency", 10)))
    max_per_asset = max(1, int(cfg.get("vision_max_images_per_asset", 4)))

    conn = get_connection()
    _ensure_schema(conn)

    # orphan cleanup: vectors whose referenced assets have all vanished
    all_asset_ids = {r[0] for r in conn.execute("SELECT id FROM assets")}
    for uid, aid in conn.execute("SELECT id, asset_id FROM image_vectors").fetchall():
        refs = {a.strip() for a in (aid or "").split(";") if a.strip()}
        if not refs or not (refs & all_asset_ids):
            conn.execute("DELETE FROM image_vectors WHERE id = ?", (uid,))
    conn.commit()

    done_urls = {r[0] for r in conn.execute("SELECT image_url FROM image_vectors")}
    work = _collect_work(max_per_asset=max_per_asset)
    pending = [(u, aids) for u, aids in work.items() if u not in done_urls]
    if limit:
        pending = pending[:limit]

    total_new = len(pending)
    stats = {"images_indexed": 0, "download_failed": 0,
             "assets_tagged": 0, "already_indexed": len(work) - total_new}
    conn.close()

    if progress:
        progress(0, total_new, f"Vision pass: 0/{total_new} images (0%)")
    print(f"[vision] {len(work)} unique images selected, {total_new} new to encode")

    if pending:
        img_model = ImageEmbedding(VISION_MODEL)
        all_vecs, asset_map = {}, {}
        inserted_this_run = []
        batch_bufs, batch_meta = [], []

        def flush():
            nonlocal inserted_this_run
            if not batch_bufs:
                return
            vecs = list(img_model.embed(list(batch_bufs)))
            conn = get_connection()
            for (u, aids), v in zip(batch_meta, vecs):
                v = np.asarray(v, dtype=np.float32)
                v /= (np.linalg.norm(v) + 1e-9)
                uid = "img_" + hashlib.sha1(u.encode()).hexdigest()
                conn.execute(
                    "INSERT OR REPLACE INTO image_vectors "
                    "(id, asset_id, image_url, vector) VALUES (?,?,?,?)",
                    (uid, ";".join(sorted(aids))[:200], u, v.tobytes()))
                all_vecs[u] = v
                asset_map.update({u: aids})
                inserted_this_run.append(u)
            conn.commit()
            conn.close()
            batch_bufs.clear(); batch_meta.clear()

        limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
        with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=15, limits=limits) as client:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                CHUNK_SIZE = 128
                done = 0
                for c_start in range(0, len(pending), CHUNK_SIZE):
                    if cancel_event is not None and cancel_event.is_set():
                        pool.shutdown(wait=False, cancel_futures=True)
                        break
                    chunk = pending[c_start:c_start + CHUNK_SIZE]
                    futures = {pool.submit(_download_worker, client, u, cancel_event): (u, aids) for u, aids in chunk}

                    for fut in as_completed(futures):
                        u, aids = futures[fut]
                        data = fut.result()
                        done += 1
                        if cancel_event is not None and cancel_event.is_set():
                            pool.shutdown(wait=False, cancel_futures=True)
                            break
                        if not data:
                            stats["download_failed"] += 1
                        else:
                            try:
                                pil = Image.open(io.BytesIO(data)).convert("RGB")
                                buf = io.BytesIO()
                                pil.save(buf, format="PNG")
                                buf.seek(0)
                                batch_bufs.append(buf)
                                batch_meta.append((u, aids))
                                if len(batch_bufs) >= batch_size:
                                    flush()
                            except Exception:
                                stats["download_failed"] += 1
                                continue
                        if progress and (done % 4 == 0 or done == total_new):
                            pct = int(done / max(total_new, 1) * 100)
                            progress(done, total_new, f"Vision pass: {done}/{total_new} images ({pct}%)")

                if not (cancel_event is not None and cancel_event.is_set()):
                    flush()  # remainder
        stats["images_indexed"] = len(inserted_this_run)

    if cancel_event is not None and cancel_event.is_set():
        invalidate_vision_cache()
        print("[vision] pass cancelled by user.")
        return stats

    if progress:
        progress(total_new, total_new, "Mining visual taxonomy concepts (Z-score calibration)…")

    # ---- concept mining over the FULL corpus (new + previously indexed) ----
    all_vecs, asset_map = {}, {}
    conn = get_connection()
    for uid, aid, u, blob in conn.execute(
            "SELECT id, asset_id, image_url, vector FROM image_vectors"):
        v = np.frombuffer(blob, dtype=np.float32)
        n = float(np.linalg.norm(v)) + 1e-9
        all_vecs[u] = v / n
        for a in (aid or "").split(";"):
            asset_map.setdefault(u, set()).add(a)
    conn.close()

    concepts, threshold_z = _load_concepts()
    if concepts:
        tags_by_asset = _mine_concepts(all_vecs, asset_map, concepts, threshold_z)
        conn = get_connection()
        all_ids = {r[0] for r in conn.execute(
            "SELECT id FROM assets WHERE source IN ('unity','fab','quixel')")}
        tagged_ids = set(tags_by_asset)
        for aid in (all_ids | tagged_ids):
            tags = tags_by_asset.get(aid, [])
            conn.execute("UPDATE assets SET vision_tags = ? WHERE id = ?",
                         (json.dumps(tags), aid))
        conn.commit()
        conn.close()
        stats["assets_tagged"] = len(tags_by_asset)

    invalidate_vision_cache()
    print(f"[vision] done: +{stats['images_indexed']} encoded, "
          f"{stats['download_failed']} failed downloads, "
          f"{stats['assets_tagged']} assets tagged, "
          f"{stats['already_indexed']} already indexed")
    return stats


import threading

_VISION_CACHE_LOCK = threading.Lock()
_VISION_MATRIX_CACHE: dict = {
    "db_path": None,
    "data_version": -1,
    "size": -1,
    "urls": None,
    "asset_map": None,
    "mat": None,
}
_clip_text_model_instance = None


def _get_clip_text_model():
    global _clip_text_model_instance
    if _clip_text_model_instance is None:
        from fastembed import TextEmbedding
        _clip_text_model_instance = TextEmbedding(CLIP_TEXT_MODEL)
    return _clip_text_model_instance


def invalidate_vision_cache():
    with _VISION_CACHE_LOCK:
        _VISION_MATRIX_CACHE["data_version"] = -1
        _VISION_MATRIX_CACHE["size"] = -1
        _VISION_MATRIX_CACHE["urls"] = None
        _VISION_MATRIX_CACHE["asset_map"] = None
        _VISION_MATRIX_CACHE["mat"] = None


def _load_vision_matrix(db_path: str = DB_PATH):
    import sqlite3
    with _VISION_CACHE_LOCK:
        conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            try:
                data_ver = conn.execute("PRAGMA data_version").fetchone()[0]
                cur_size = conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]
            except Exception:
                _ensure_schema(conn)
                data_ver = conn.execute("PRAGMA data_version").fetchone()[0]
                cur_size = conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]

            if (_VISION_MATRIX_CACHE["urls"] is not None and
                _VISION_MATRIX_CACHE["db_path"] == db_path and
                _VISION_MATRIX_CACHE["data_version"] == data_ver and
                _VISION_MATRIX_CACHE["size"] == cur_size):
                return _VISION_MATRIX_CACHE["urls"], _VISION_MATRIX_CACHE["asset_map"], _VISION_MATRIX_CACHE["mat"]

            urls, asset_map, blobs = [], {}, []
            for uid, aid, u, blob in conn.execute("SELECT id, asset_id, image_url, vector FROM image_vectors ORDER BY id"):
                urls.append(u)
                asset_map[u] = [a.strip() for a in (aid or "").split(";") if a.strip()]
                blobs.append(blob)

            if not urls:
                return None, None, None

            mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(urls), -1).copy()
            mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)

            _VISION_MATRIX_CACHE["db_path"] = db_path
            _VISION_MATRIX_CACHE["data_version"] = data_ver
            _VISION_MATRIX_CACHE["size"] = len(urls)
            _VISION_MATRIX_CACHE["urls"] = urls
            _VISION_MATRIX_CACHE["asset_map"] = asset_map
            _VISION_MATRIX_CACHE["mat"] = mat
            return urls, asset_map, mat
        finally:
            conn.close()


def vision_search(query: str, k: int = 40, db_path: str = DB_PATH) -> list:
    """Cross-modal text-to-image search: scores natural language query against
    all screenshot vectors using CLIP, returning top-k assets with their best-matching screenshot."""
    urls, asset_map, mat = _load_vision_matrix(db_path)
    if urls is None or mat is None or not urls:
        return []

    try:
        qv = _embed_clip_query(query)
        scores = mat @ qv  # (N_images,) dot product

        # Aggregate max score per asset_id
        best_scores = {}
        best_images = {}
        for idx, s in enumerate(scores):
            u = urls[idx]
            aids = asset_map.get(u, [])
            for aid in aids:
                if aid not in best_scores or s > best_scores[aid]:
                    best_scores[aid] = float(s)
                    best_images[aid] = u

        top_aids = sorted(best_scores.keys(), key=lambda a: -best_scores[a])[:k]
        return [{"id": a, "score": round(best_scores[a], 4), "image_url": best_images[a]} for a in top_aids]
    except Exception as e:
        print(f"[vision] search failed: {e}", file=sys.stderr)
        return []


@functools.lru_cache(maxsize=512)
def _embed_clip_query(query: str):
    model = _get_clip_text_model()
    qv = np.array(list(model.embed([query]))[0], dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    return qv


def status():
    conn = get_connection()
    total_assets = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE source IN ('unity','fab','quixel')").fetchone()[0]
    with_covers = conn.execute(
        "SELECT COUNT(*) FROM assets WHERE source IN ('unity','fab','quixel') AND image_url != ''").fetchone()[0]
    try:
        vecs = conn.execute("SELECT COUNT(*) FROM image_vectors").fetchone()[0]
    except Exception:
        vecs = 0
    tagged = 0
    try:
        tagged = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE source IN ('unity','fab','quixel') AND "
            "vision_tags IS NOT NULL AND vision_tags != '' AND vision_tags != '[]'").fetchone()[0]
    except Exception:
        pass
    conn.close()
    print(f"assets={total_assets}  with_cover={with_covers}  "
          f"image_vectors={vecs}  assets_with_vision_tags={tagged}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])

    if cmd == "build":
        build(limit=lim)
    elif cmd == "status":
        status()
    elif cmd == "query" and len(sys.argv) > 2:
        res = vision_search(sys.argv[2])
        print(f"Found {len(res)} visual matches for '{sys.argv[2]}':")
        for r in res[:10]:
            print(f"  {r['id']} (score: {r['score']}) -> {r['image_url']}")
    else:
        print("Usage:\n  python -m src.vision build [--limit N]\n  python -m src.vision query <text>\n  python -m src.vision status")
