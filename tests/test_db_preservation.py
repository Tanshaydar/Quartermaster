import os
import sqlite3
import pytest
import tempfile
from src.db import init_db, upsert_asset, mark_enriched, get_connection, search_assets


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_p = os.path.join(tmpdir, "test_assets.db")
        init_db(db_p)
        yield db_p


def test_upsert_preservation(temp_db):
    test_id = "test_preservation_001"
    
    # 1. Initial raw store fetch
    upsert_asset({
        "id": test_id,
        "source": "unity",
        "title": "Hyper Realistic Ocean Water System",
        "summary": "Initial short summary",
        "usage_notes": "Initial short notes",
        "enriched": 0
    }, db_path=temp_db)

    # 2. Enriched with deep metadata
    mark_enriched(test_id,
                  summary="High performance compute-shader FFT ocean simulation with buoyancy",
                  usage_notes="Requires compute shader support; HDRP ready",
                  video_links=["https://youtube.com/watch?v=ocean123456"],
                  db_path=temp_db)

    # 3. Store re-fetch with empty / fallback summary
    upsert_asset({
        "id": test_id,
        "source": "unity",
        "title": "Hyper Realistic Ocean Water System",
        "summary": "Fallback summary from classifier",
        "usage_notes": "Fallback notes",
        "enriched": 0
    }, db_path=temp_db)

    conn = get_connection(temp_db)
    row = conn.execute("SELECT summary, usage_notes, video_links, enriched FROM assets WHERE id = ?", (test_id,)).fetchone()
    assert row["summary"] == "High performance compute-shader FFT ocean simulation with buoyancy"
    assert row["usage_notes"] == "Requires compute shader support; HDRP ready"
    assert "ocean123456" in row["video_links"]
    assert row["enriched"] == 1

    # 4. FTS search must match the preserved enriched text
    fts_hits = conn.execute("SELECT id FROM assets_fts WHERE assets_fts MATCH 'simulation'").fetchall()
    assert any(r[0] == test_id for r in fts_hits)
    conn.close()


def test_fts_migration_v2(temp_db):
    conn = get_connection(temp_db)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2
    conn.close()
