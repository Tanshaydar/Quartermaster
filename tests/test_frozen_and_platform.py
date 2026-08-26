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


if __name__ == "__main__":
    unittest.main()
