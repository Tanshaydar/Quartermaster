"""
VaultMCP hybrid semantic search (offline, CPU-only).

Uses fastembed (ONNX, no API calls) with a small bi-encoder to embed every
vault asset once; vectors live in a regular SQLite table and are brute-force
cosine-scored at query time — instant at vault scale (a few thousand assets),
no vector-extension dependency needed.

Hybrid strategy: Reciprocal Rank Fusion of FTS5/BM25 keyword results and
semantic results. Keyword hits keep their precision; semantic catches
natural-language intent ("spooky abandoned industrial site" -> Warehouse pack).

CLI:
  python -m src.semantic build      # (re)build the embedding index
  python -m src.semantic query "spooky abandoned industrial site"
"""
import json
import os
import struct
import sys
from typing import Any, Dict, List, Optional

try:
    from .db import get_connection, search_assets, DB_PATH
    from .config import load_config
except ImportError:
    from db import get_connection, search_assets, DB_PATH
    from config import load_config

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"   # 384-dim, ~33MB quantized, CPU-fast


_model_instance = None


def _model():
    global _model_instance
    if _model_instance is None:
        cfg = load_config()
        from fastembed import TextEmbedding
        _model_instance = TextEmbedding(model_name=cfg.get("embedding_model", DEFAULT_MODEL))
    return _model_instance


def _doc_text(a: Dict[str, Any]) -> str:
    parts = [
        a.get("title", ""),
        a.get("publisher") or "",
        a.get("category") or "",
        " ".join(a.get("tags") or []),
        " ".join(a.get("render_pipelines") or []),
        a.get("summary") or "",
        a.get("usage_notes") or "",
    ]
    return ". ".join(p for p in parts if p)[:1200]


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asset_vectors (
            asset_id TEXT PRIMARY KEY,
            dim INTEGER NOT NULL,
            vec BLOB NOT NULL
        )
    """)
    conn.commit()


def index_size(db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    _ensure_table(conn)
    n = conn.execute("SELECT COUNT(*) FROM asset_vectors").fetchone()[0]
    conn.close()
    return n


def build_index(batch_size: int = 64, db_path: str = DB_PATH) -> int:
    """Embed all assets. Returns number indexed."""
    model = _model()
    conn = get_connection(db_path)
    _ensure_table(conn)

    cur = conn.execute(
        "SELECT id, title, publisher, category, tags, summary, usage_notes, render_pipelines FROM assets")
    rows = [dict(r) for r in cur.fetchall()]
    print(f"[semantic] embedding {len(rows)} assets…")

    docs = [_doc_text(r) for r in rows]
    done = 0
    for start in range(0, len(docs), batch_size):
        batch = docs[start:start + batch_size]
        vecs = list(model.embed(batch))
        for r, v in zip(rows[start:start + batch_size], vecs):
            blob = struct.pack(f"{len(v)}f", *[float(x) for x in v])
            conn.execute(
                "INSERT INTO asset_vectors (asset_id, dim, vec) VALUES (?,?,?) "
                "ON CONFLICT(asset_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec",
                (r["id"], len(v), blob))
        done += len(batch)
        if done % 256 < batch_size:
            print(f"  {done}/{len(docs)}")
    conn.commit()

    # drop vectors for assets that disappeared
    ids = set(r["id"] for r in rows)
    stale = [aid for (aid,) in conn.execute("SELECT asset_id FROM asset_vectors")
             if aid not in ids]
    conn.executemany("DELETE FROM asset_vectors WHERE asset_id = ?", [(a,) for a in stale])
    conn.commit()
    conn.close()
    invalidate_vector_cache()
    print(f"[semantic] indexed {done} assets.")
    return done


import threading

_CACHE_LOCK = threading.Lock()

# In-memory vector matrix cache keyed on (PRAGMA data_version, COUNT(*))
# Thread-safe across FastMCP anyio worker threads and GUI background loaders
_MATRIX_CACHE: Dict[str, Any] = {
    "db_path": None,
    "data_version": -1,
    "size": -1,
    "ids": None,
    "mat": None,
    "np": None,
}


def invalidate_vector_cache():
    with _CACHE_LOCK:
        _MATRIX_CACHE["data_version"] = -1
        _MATRIX_CACHE["size"] = -1
        _MATRIX_CACHE["ids"] = None
        _MATRIX_CACHE["mat"] = None


def _load_matrix(db_path: str = DB_PATH):
    import sqlite3
    with _CACHE_LOCK:
        # Open lightweight thread-safe autocommit reader
        conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            try:
                data_ver = conn.execute("PRAGMA data_version").fetchone()[0]
                cur_size = conn.execute("SELECT COUNT(*) FROM asset_vectors").fetchone()[0]
            except Exception:
                _ensure_table(conn)
                data_ver = conn.execute("PRAGMA data_version").fetchone()[0]
                cur_size = conn.execute("SELECT COUNT(*) FROM asset_vectors").fetchone()[0]

            if (_MATRIX_CACHE["ids"] is not None and
                _MATRIX_CACHE["db_path"] == db_path and
                _MATRIX_CACHE["data_version"] == data_ver and
                _MATRIX_CACHE["size"] == cur_size):
                return _MATRIX_CACHE["ids"], _MATRIX_CACHE["mat"], _MATRIX_CACHE["np"]

            ids, dims, blobs = [], [], []
            for aid, dim, vec in conn.execute("SELECT asset_id, dim, vec FROM asset_vectors ORDER BY asset_id"):
                ids.append(aid)
                dims.append(dim)
                blobs.append(vec)

            if not ids:
                return None, None, None

            import numpy as np
            if len(set(dims)) > 1:
                # Mixed dimensions detected (e.g. model switched in config)
                print("[semantic] Mixed vector dimensions detected in database; re-indexing required.")
                return None, None, None
            mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(ids), dims[0]).copy()
            mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)

            _MATRIX_CACHE["db_path"] = db_path
            _MATRIX_CACHE["data_version"] = data_ver
            _MATRIX_CACHE["size"] = len(ids)
            _MATRIX_CACHE["ids"] = ids
            _MATRIX_CACHE["mat"] = mat
            _MATRIX_CACHE["np"] = np
            return ids, mat, np
        finally:
            conn.close()


def semantic_search(query: str, k: int = 40, db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Top-k assets by cosine similarity to the natural-language query."""
    total_assets = 0
    conn = get_connection(db_path)
    total_assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    conn.close()
    # Non-blocking: if index is empty, return empty and let hybrid_search fallback to keyword mode
    if index_size(db_path) == 0:
        return []

    ids, mat, np = _load_matrix(db_path)
    if ids is None:
        return []
    model = _model()
    qv = np.array(list(model.embed([query]))[0], dtype=np.float32)
    qv /= (np.linalg.norm(qv) + 1e-9)
    scores = mat @ qv
    top = np.argsort(-scores)[:k]
    out = [{"id": ids[i], "score": float(scores[i])} for i in top]
    return out


def hybrid_search(query: str, limit: int = 25, db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    RRF-fused keyword + semantic search.
    Returns items plus per-item match info so agents can see WHY something hit.
    """
    kw = search_assets(query=query, limit=50, db_path=db_path)
    kw_ids = [r["id"] for r in kw]

    conn = get_connection(db_path)
    _ensure_table(conn)
    total_assets = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    total_vectors = conn.execute("SELECT COUNT(*) FROM asset_vectors").fetchone()[0]
    conn.close()

    if total_vectors == 0:
        return {
            "count": len(kw[:limit]),
            "search_mode": "keyword-only",
            "results": kw[:limit],
            "note": "Semantic index is unbuilt (0 vectors). Run 'python -m src.semantic build' to enable AI vector search."
        }

    try:
        sem = semantic_search(query, k=50, db_path=db_path)
    except Exception as e:
        return {"count": len(kw[:limit]), "search_mode": "keyword-only", "results": kw[:limit],
                "note": f"semantic index unavailable: {e}"}

    K = 60
    fused: Dict[str, float] = {}
    why: Dict[str, str] = {}
    for rank, r in enumerate(kw):
        fused[r["id"]] = fused.get(r["id"], 0) + 1 / (K + rank + 1)
        why[r["id"]] = "keyword"
    sem_map = {}
    for rank, s in enumerate(sem):
        fused[s["id"]] = fused.get(s["id"], 0) + 1 / (K + rank + 1)
        sem_map[s["id"]] = s["score"]
        why[s["id"]] = "both" if s["id"] in why else "semantic"
    ordered = sorted(fused.items(), key=lambda x: -x[1])[:limit]

    # hydrate
    by_id = {r["id"]: r for r in kw}
    missing = [i for i, _ in ordered if i not in by_id]
    if missing:
        conn = get_connection(db_path)
        qmarks = ",".join("?" * len(missing))
        for row in conn.execute(f"SELECT * FROM assets WHERE id IN ({qmarks})", missing):
            item = dict(row)
            for k2 in ("render_pipelines", "tags", "gallery_images", "video_links", "formats"):
                try:
                    item[k2] = json.loads(item.get(k2) or "[]")
                except Exception:
                    item[k2] = []
            by_id[item["id"]] = item
        conn.close()

    results = []
    for i, score in ordered:
        if i not in by_id:
            continue
        item = dict(by_id[i])
        item["match"] = why[i]
        item["relevance"] = round(score, 4)
        if why[i] in ("semantic", "both"):
            item["sem_score"] = round(sem_map.get(i, 0.0), 4)
        results.append(item)

    mode = "hybrid" if any(why[i] != "keyword" for i, _ in ordered) else "keyword"
    ret = {"count": len(results), "search_mode": mode, "results": results}
    if total_vectors < total_assets:
        ret["note"] = f"Partial vector coverage ({total_vectors}/{total_assets} indexed). Run 'python -m src.semantic build' to index recent additions."
    return ret


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "build":
        build_index()
    elif cmd == "query" and len(sys.argv) > 2:
        res = hybrid_search(sys.argv[2])
        print(f"mode: {res['mode']}")
        for r in res["results"][:10]:
            print(f"  [{r['match']:8s}] {r['title'][:60]}")
    else:
        print("Usage:\n  python -m src.semantic build\n  python -m src.semantic query \"text\"")
