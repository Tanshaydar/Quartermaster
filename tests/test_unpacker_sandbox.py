import os
import unittest
import tempfile
from src.unpacker import _sanitize_package_path, _safe_target, _strip_reason


class TestUnpackerSandbox(unittest.TestCase):
    def test_sanitize_path_traversal(self):
        rel, parts = _sanitize_package_path("../../../etc/passwd")
        self.assertNotIn("..", parts)
        self.assertIn("passwd", parts)

    def test_sanitize_drive_letters(self):
        rel, parts = _sanitize_package_path("C:/Assets/Textures/Rock_01.png")
        self.assertEqual(parts, ["Textures", "Rock_01.png"])

        rel2, parts2 = _sanitize_package_path("Assets/Audio/Track:Reverb.wav")
        self.assertEqual(parts2, ["Audio", "Track:Reverb.wav"])

    def test_sanitize_null_bytes_and_control_chars(self):
        rel, parts = _sanitize_package_path("Assets/Models/Hero\x00\x01\x1fCharacter.fbx")
        self.assertNotIn("\x00", rel)
        self.assertNotIn("\x01", rel)
        self.assertEqual(parts, ["Models", "HeroCharacter.fbx"])

    def test_safe_target_enforcement(self):
        with tempfile.TemporaryDirectory() as tmp_proj:
            target = _safe_target(tmp_proj, ["Models", "Sword.fbx"])
            self.assertTrue(target.startswith(os.path.abspath(tmp_proj)))
            self.assertTrue(target.endswith(os.path.join("Assets", "Models", "Sword.fbx")))

    def test_strip_demo_rules(self):
        strip_dirs = {"demo", "demos", "samples", "sample", "example", "examples", "test"}
        strip_exts = {".unity", ".mp4", ".mov", ".avi"}

        self.assertIsNotNone(_strip_reason("Assets/Hero/Demo/Scene.unity", strip_dirs, strip_exts))
        self.assertIsNotNone(_strip_reason("Assets/Hero/Textures/Trailer.mp4", strip_dirs, strip_exts))
        self.assertIsNone(_strip_reason("Assets/Hero/Models/Hero.fbx", strip_dirs, strip_exts))


if __name__ == "__main__":
    unittest.main()
