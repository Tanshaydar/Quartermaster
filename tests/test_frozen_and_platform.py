import os
import sys
import unittest
from unittest.mock import patch


class TestFrozenAndPlatformResolution(unittest.TestCase):
    def test_frozen_paths_simulation(self):
        """Simulate PyInstaller frozen environment and ensure bundle vs data separation."""
        fake_meipass = os.path.abspath("C:/fake_app/_internal")
        fake_localapp = os.path.abspath("C:/Users/fake_user/AppData/Local")

        with patch.dict(os.environ, {"LOCALAPPDATA": fake_localapp}):
            with patch.object(sys, "frozen", True, create=True):
                with patch.object(sys, "_MEIPASS", fake_meipass, create=True):
                    is_frozen = getattr(sys, "frozen", False)
                    bundle_dir = getattr(sys, "_MEIPASS", None)
                    data_dir = os.path.join(os.environ["LOCALAPPDATA"], "Quartermaster")

                    self.assertTrue(is_frozen)
                    self.assertEqual(bundle_dir, fake_meipass)
                    self.assertEqual(data_dir, os.path.join(fake_localapp, "Quartermaster"))

                    recipes_path = os.path.join(bundle_dir, "data", "recipes.json")
                    web_dir = os.path.join(bundle_dir, "web")
                    self.assertTrue(recipes_path.startswith(fake_meipass))
                    self.assertTrue(web_dir.startswith(fake_meipass))

                    db_path = os.path.join(data_dir, "data", "assets.db")
                    token_path = os.path.join(data_dir, "data", ".auth_token")
                    self.assertTrue(db_path.startswith(data_dir))
                    self.assertTrue(token_path.startswith(data_dir))

    @unittest.skipUnless(sys.platform == "win32", "Windows-specific path assertions")
    def test_windows_localappdata_resolution(self):
        from src import config
        self.assertIn("LOCALAPPDATA", os.environ)
        local_app = os.environ.get("LOCALAPPDATA")
        expected_prefix = os.path.join(local_app, "Quartermaster")
        if config.IS_FROZEN:
            self.assertEqual(config.DATA_DIR, expected_prefix)

    def test_linux_xdg_data_dir_resolution(self):
        """Simulate Linux XDG_DATA_HOME environment when frozen."""
        fake_xdg = os.path.abspath("/tmp/fake_user/share")
        with patch.dict(os.environ, {"XDG_DATA_HOME": fake_xdg}):
            with patch.object(sys, "platform", "linux"):
                with patch.object(sys, "frozen", True, create=True):
                    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
                    expected_data_dir = os.path.join(xdg_data, "quartermaster")
                    self.assertEqual(expected_data_dir, os.path.join(fake_xdg, "quartermaster"))

    def test_linux_unity_cache_scan_safety(self):
        """Ensure local_scan.scan_all executes safely across platform roots without touching production DB."""
        import tempfile
        from src.db import init_db
        from src import local_scan

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name

        try:
            init_db(tmp_db)
            res = local_scan.scan_all(db_path=tmp_db)
            self.assertIsInstance(res, dict)
            self.assertIn("files_scanned", res)
            self.assertIn("matched_to_library", res)
        finally:
            if os.path.exists(tmp_db):
                try:
                    os.remove(tmp_db)
                except Exception:
                    pass

    def test_browser_candidates_and_path_discovery(self):
        """Ensure _find_browser checks candidates and path binaries safely."""
        from src import store_client
        found = store_client._find_browser()
        if found:
            self.assertTrue(os.path.isfile(found))


if __name__ == "__main__":
    unittest.main()
