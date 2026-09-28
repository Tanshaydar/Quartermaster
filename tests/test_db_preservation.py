import os
import unittest
import tempfile
from src.db import init_db, upsert_asset, mark_enriched, get_connection, search_assets
from src.ingest import _row_from_unity


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

        # 2. Enrich the asset with rich description, usage notes, gallery images and tags
        mark_enriched(
            test_id,
            summary="High performance compute-shader FFT ocean simulation with buoyancy",
            usage_notes="Requires compute shader support; HDRP ready",
            video_links=["https://www.youtube.com/watch?v=ocean123456"],
            gallery_images=["https://img.com/1.png", "https://img.com/2.png", "https://img.com/3.png"],
            tags=["ocean", "water", "simulation"],
            db_path=self.db_path
        )

        # 3. Simulate subsequent store fetch / disk scan upsert with shallow stub
        upsert_asset({
            "id": test_id,
            "source": "unity",
            "title": "Ocean Simulator Pro",
            "summary": "Basic ocean rendering package",
            "gallery_images": ["https://img.com/1.png"],
            "tags": ["ocean"],
            "enriched": 0,
        }, db_path=self.db_path)

        conn = get_connection(self.db_path)
        row = conn.execute("SELECT summary, usage_notes, video_links, gallery_images, tags, enriched FROM assets WHERE id = ?", (test_id,)).fetchone()
        self.assertEqual(row["summary"], "High performance compute-shader FFT ocean simulation with buoyancy")
        self.assertEqual(row["usage_notes"], "Requires compute shader support; HDRP ready")
        self.assertIn("ocean123456", row["video_links"])
        self.assertIn("https://img.com/2.png", row["gallery_images"])
        self.assertIn("simulation", row["tags"])
        self.assertEqual(row["enriched"], 1)

        # 4. FTS search must match the preserved enriched text and tags
        fts_hits = conn.execute("SELECT id FROM assets_fts WHERE assets_fts MATCH 'simulation'").fetchall()
        self.assertTrue(any(r[0] == test_id for r in fts_hits))
        conn.close()

    def test_fts_migration_v2(self):
        conn = get_connection(self.db_path)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertGreaterEqual(ver, 2)
        conn.close()


class TestSizeAndDatePreservation(unittest.TestCase):
    """Only the Unity CSV import carries sizes, and store syncs send none. upsert_asset() still
    overwrote size_mb, size_str and claimed_date unconditionally, so each sync reset CSV sizes to 0
    and blanked CSV dates (Unity's library payload has no acquisition date). On the real vault all
    8,045 assets ended up at size 0, leaving "Size (Largest)" to sort by title alone."""

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

    def _csv_import(self, pkg_id, title, size, date):
        upsert_asset(_row_from_unity({"Package ID": pkg_id, "Asset Name": title, "Size": size,
                                      "Claimed/Grant Date": date}), db_path=self.db_path)

    def _store_sync(self, pkg_id, title):
        # Shaped like fetch_library()'s record: no size keys, and Unity items carry no createdAt
        upsert_asset({"id": f"unity_{pkg_id}", "source": "unity", "package_id": pkg_id, "title": title,
                      "claimed_date": "", "store_url": f"https://assetstore.unity.com/packages/x-{pkg_id}"},
                     db_path=self.db_path)

    def _stored(self, asset_id):
        conn = get_connection(self.db_path)
        try:
            return tuple(conn.execute("SELECT size_mb, size_str, claimed_date FROM assets WHERE id = ?",
                                      (asset_id,)).fetchone())
        finally:
            conn.close()

    def test_store_sync_keeps_csv_size_and_date(self):
        self._csv_import("396300", "Canyons - StampIT!", "4.50 GB", "2026-08-21")
        self._store_sync("396300", "Canyons - StampIT!")
        self.assertEqual(self._stored("unity_396300"), (4608.0, "4.50 GB", "2026-08-21"))

    def test_zero_or_null_values_keep_existing(self):
        # sync_quixel_catalog() sends size_mb=0.0 and size_str="" outright; NULLs must not win either
        self._csv_import("396300", "Canyons - StampIT!", "371.23 MB", "2026-08-21")
        for blank in ({"size_mb": 0.0, "size_str": "", "claimed_date": ""},
                      {"size_mb": None, "size_str": None, "claimed_date": None}):
            with self.subTest(**blank):
                upsert_asset({"id": "unity_396300", "source": "unity", "title": "Canyons - StampIT!", **blank},
                             db_path=self.db_path)
                self.assertEqual(self._stored("unity_396300"), (371.23, "371.23 MB", "2026-08-21"))

    def test_new_size_and_date_replace_old(self):
        self._csv_import("396300", "Canyons - StampIT!", "371.23 MB", "2026-08-21")
        self._csv_import("396300", "Canyons - StampIT!", "1.02 GB", "2026-09-01")
        self.assertEqual(self._stored("unity_396300"), (1044.48, "1.02 GB", "2026-09-01"))

    def test_size_sort_survives_store_sync(self):
        # Title order differs from size order, so a sort that fell back to the title tie-break fails
        for pkg_id, title, size in (("1", "Alpha Rocks", "12.00 MB"), ("2", "Beta Cliffs", "4.50 GB"),
                                    ("3", "Gamma Dunes", "371.23 MB")):
            self._csv_import(pkg_id, title, size, "2026-08-21")
            self._store_sync(pkg_id, title)
        ids = [a["id"] for a in search_assets(sort_by="size_desc", db_path=self.db_path)]
        self.assertEqual(ids, ["unity_2", "unity_3", "unity_1"])


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
