import os
import tempfile
import unittest
from unittest.mock import patch

from src import local_scan
from src.db import init_db, upsert_asset, get_connection, search_assets
from src.ingest import _format_size_mb, _parse_size_mb

MB = 1024 * 1024
QUIXEL_ID = "quixel_b8cfda6a-0000-4000-8000-000000000000"


def _make_file(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


class TestSizeFormat(unittest.TestCase):
    def test_format_matches_unity_csv_and_round_trips(self):
        # Strings as the Unity CSV export writes them: 2 decimals, GB from 1024 MB up
        for size_str in ("0.23 MB", "371.23 MB", "1021.68 MB", "1.02 GB", "4.50 GB"):
            with self.subTest(size_str=size_str):
                self.assertEqual(_format_size_mb(_parse_size_mb(size_str)), size_str)


class TestScanRecordsDiskSizes(unittest.TestCase):
    """Only the Unity CSV import ever set a size, so on the real vault every asset sat at size 0 and
    "Size (Largest)" sorted by title alone. The disk scan knows where each download lives: it now
    records the .unitypackage's file size, or the total of a Fab vault folder."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = self.tmpdir.name
        self.db_path = os.path.join(root, "test_assets.db")
        init_db(self.db_path)

        # %APPDATA%/Unity/Asset Store-5.x/<Publisher>/<Category>/<Title>.unitypackage
        self.appdata = os.path.join(root, "appdata")
        cache = os.path.join(self.appdata, "Unity", "Asset Store-5.x", "Rowlan", "Terrain")
        _make_file(os.path.join(cache, "Canyons - StampIT!.unitypackage"), 3 * MB)
        _make_file(os.path.join(cache, "Empty Download.unitypackage"), 0)
        _make_file(os.path.join(cache, "Orphan Tool.unitypackage"), 3 * MB // 2)
        # <vault>/FabLibrary/<Slug>-<first 8 hex of the listing uid>/ holds the downloaded files
        self.vault = os.path.join(root, "VaultCache", "FabLibrary")
        folder = os.path.join(self.vault, "Castle_Wall-b8cfda6a")
        _make_file(os.path.join(folder, "fbx", "high", "metadata"), MB)
        _make_file(os.path.join(folder, "fbx", "high", "thumbnail.jpeg"), MB // 2)
        _make_file(os.path.join(folder, "textures", "albedo.png"), 2 * MB)

        # CSV sizes for two of the Unity packages; the Quixel store sync never has one
        upsert_asset({"id": "unity_1", "source": "unity", "title": "Canyons - StampIT!",
                      "size_mb": 373.25, "size_str": "373.25 MB"}, db_path=self.db_path)
        upsert_asset({"id": "unity_2", "source": "unity", "title": "Empty Download",
                      "size_mb": 12.5, "size_str": "12.50 MB"}, db_path=self.db_path)
        upsert_asset({"id": QUIXEL_ID, "source": "quixel", "title": "Castle Wall"}, db_path=self.db_path)

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmpdir.cleanup()
        except Exception:
            pass

    def _scan(self):
        home = os.path.join(self.tmpdir.name, "home")   # keeps the Linux and macOS cache roots empty
        with patch.dict(os.environ, {"APPDATA": self.appdata, "USERPROFILE": home, "HOME": home}), \
                patch("src.local_scan.load_config", return_value={"fab_vault_dirs": [self.vault]}):
            return local_scan.scan_all(db_path=self.db_path)

    def _sizes(self):
        conn = get_connection(self.db_path)
        try:
            return {r["title"]: (r["size_mb"], r["size_str"])
                    for r in conn.execute("SELECT title, size_mb, size_str FROM assets")}
        finally:
            conn.close()

    def test_scan_records_what_each_download_takes_on_disk(self):
        result = self._scan()
        self.assertEqual(result["files_scanned"], {"unity": 3, "fab": 1})
        self.assertEqual(result["adopted_from_disk"], 1)
        self.assertEqual(self._sizes(), {
            "Canyons - StampIT!": (3.0, "3.00 MB"),   # the package on disk replaces the CSV size
            "Empty Download": (12.5, "12.50 MB"),     # an empty download keeps the size it had
            "Orphan Tool": (1.5, "1.50 MB"),          # adopted from disk, with its size
            "Castle Wall": (3.5, "3.50 MB"),          # every file under the vault folder
        })
        titles = [a["title"] for a in search_assets(sort_by="size_desc", db_path=self.db_path)]
        self.assertEqual(titles, ["Empty Download", "Castle Wall", "Canyons - StampIT!", "Orphan Tool"])

    def test_store_sync_after_scan_keeps_measured_size(self):
        self._scan()
        # sync_quixel_catalog() sends size_mb=0.0 and size_str="" for every listing
        upsert_asset({"id": QUIXEL_ID, "source": "quixel", "title": "Castle Wall", "size_mb": 0.0,
                      "size_str": ""}, db_path=self.db_path)
        self.assertEqual(self._sizes()["Castle Wall"], (3.5, "3.50 MB"))

    def test_disk_size_of_vanished_download_is_zero(self):
        self.assertEqual(local_scan._disk_size_mb(os.path.join(self.tmpdir.name, "gone.unitypackage")), 0.0)


if __name__ == "__main__":
    unittest.main()
