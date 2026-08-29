import os
import unittest
import tempfile
from src.db import init_db, upsert_asset, mark_enriched, get_connection, search_assets


class TestDbPreservation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def test_upsert_preservation(self):
        # 1. Insert initial minimal asset
        test_id = "unity_ocean_pack_1"
        upsert_asset({
            "id": test_id,
            "source": "unity",
            "title": "Ocean Simulator Pro",
            "summary": "Basic ocean rendering package",
            "enriched": 0,
        }, db_path=self.db_path)

        # 2. Enrich the asset with rich description and usage notes
        mark_enriched(
            test_id,
            summary="High performance compute-shader FFT ocean simulation with buoyancy",
            usage_notes="Requires compute shader support; HDRP ready",
            video_links=["https://www.youtube.com/watch?v=ocean123456"],
            db_path=self.db_path
        )

        # 3. Simulate subsequent store fetch / disk scan upsert
        upsert_asset({
            "id": test_id,
            "source": "unity",
            "title": "Ocean Simulator Pro",
            "summary": "Basic ocean rendering package",
            "enriched": 0,
        }, db_path=self.db_path)

        conn = get_connection(self.db_path)
        row = conn.execute("SELECT summary, usage_notes, video_links, enriched FROM assets WHERE id = ?", (test_id,)).fetchone()
        self.assertEqual(row["summary"], "High performance compute-shader FFT ocean simulation with buoyancy")
        self.assertEqual(row["usage_notes"], "Requires compute shader support; HDRP ready")
        self.assertIn("ocean123456", row["video_links"])
        self.assertEqual(row["enriched"], 1)

        # 4. FTS search must match the preserved enriched text
        fts_hits = conn.execute("SELECT id FROM assets_fts WHERE assets_fts MATCH 'simulation'").fetchall()
        self.assertTrue(any(r[0] == test_id for r in fts_hits))
        conn.close()

    def test_fts_migration_v2(self):
        conn = get_connection(self.db_path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(ver, 2)
        conn.close()


class TestImageVectorPrune(unittest.TestCase):
    """init_db() prunes dangling image_vectors, but asset_id holds a ';'-joined
    list when several assets share one cover image. A naive `NOT IN (SELECT id
    FROM assets)` deletes every shared-cover row even when its assets are alive,
    silently discarding valid embeddings on each startup."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_assets.db")
        init_db(self.db_path)
        for i in (1, 2):
            upsert_asset({"id": f"unity_{i}", "source": "unity",
                          "title": f"Asset {i}"}, db_path=self.db_path)

        conn = get_connection(self.db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS image_vectors (
            id TEXT PRIMARY KEY, asset_id TEXT, image_url TEXT, vector BLOB)""")
        conn.executemany(
            "INSERT OR REPLACE INTO image_vectors (id, asset_id, image_url, vector)"
            " VALUES (?,?,?,?)",
            [("v_live_single", "unity_1", "http://x/1", b"\x00" * 4),
             ("v_live_joined", "unity_1;unity_2", "http://x/2", b"\x00" * 4),
             ("v_half_joined", "unity_GONE;unity_2", "http://x/3", b"\x00" * 4),
             ("v_dead_single", "unity_GONE", "http://x/4", b"\x00" * 4),
             ("v_dead_joined", "unity_GONE_A;unity_GONE_B", "http://x/5", b"\x00" * 4)])
        conn.commit()
        conn.close()

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def test_shared_cover_vectors_survive_prune(self):
        init_db(self.db_path)  # re-run: this is what happens on every startup

        conn = get_connection(self.db_path)
        rows = {r[0] for r in conn.execute("SELECT id FROM image_vectors")}
        conn.close()

        # a row survives if ANY referenced asset still exists
        self.assertIn("v_live_single", rows)
        self.assertIn("v_live_joined", rows, "shared-cover vector was wrongly pruned")
        self.assertIn("v_half_joined", rows, "partially-live shared cover was wrongly pruned")
        # ...and is removed only when every reference is gone
        self.assertNotIn("v_dead_single", rows)
        self.assertNotIn("v_dead_joined", rows)
