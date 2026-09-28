import io
import os
import tarfile
import unittest
import tempfile
from unittest.mock import patch
from src.unpacker import (_sanitize_package_path, _safe_target, _strip_reason,
                          _embedded_package_path, _safe_package_target, unpack_unitypackage)

PROJECT_MANIFEST = '{"dependencies": {}}'
UPM_MANIFEST = b'{"name": "com.example.tool", "version": "1.0.0"}'


def _build_unitypackage(path: str, entries: dict) -> None:
    """Writes a .unitypackage from declared pathname -> asset bytes (None = folder entry)."""
    with tarfile.open(path, "w:gz") as tf:
        for i, (declared, data) in enumerate(entries.items()):
            guid = f"{i:032x}"
            files = {"pathname": (declared + "\n00").encode(),
                     "asset.meta": f"fileFormatVersion: 2\nguid: {guid}\n".encode()}
            if data is not None:
                files["asset"] = data
            for name, payload in files.items():
                info = tarfile.TarInfo(f"{guid}/{name}")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))


def _tree(root: str) -> set:
    """All files under root, as forward-slash paths relative to root."""
    return {os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
            for d, _, files in os.walk(root) for f in files}


class TestUnpackerSandbox(unittest.TestCase):
    def _unpack(self, tmp, entries):
        """Unpacks entries into <tmp>/Project, a fresh project with Assets/ and Packages/manifest.json."""
        pkg_path = os.path.join(tmp, "Test.unitypackage")
        _build_unitypackage(pkg_path, entries)
        project = os.path.join(tmp, "Project")
        os.makedirs(os.path.join(project, "Assets"))
        os.makedirs(os.path.join(project, "Packages"))
        with open(os.path.join(project, "Packages", "manifest.json"), "w") as f:
            f.write(PROJECT_MANIFEST)
        with patch("src.unpacker.load_config", return_value={}):
            result = unpack_unitypackage(pkg_path, project)
        return project, result

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

    def test_embedded_upm_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, result = self._unpack(tmp, {
                "Packages/com.example.tool": None,  # package root folder entry
                "Packages/com.example.tool/package.json": UPM_MANIFEST,
                "Packages/com.example.tool/Runtime": None,
                "Packages/com.example.tool/Runtime/Tool.cs": b"class Tool {}",
                "Assets/Tool/Readme.txt": b"readme",
            })
            self.assertEqual(result["embedded_packages"], ["com.example.tool"])
            # per-file .meta kept; no Packages/com.example.tool.meta, nothing relocated
            self.assertEqual(_tree(project), {
                "Assets/Tool/Readme.txt",
                "Assets/Tool/Readme.txt.meta",
                "Packages/manifest.json",
                "Packages/com.example.tool/package.json",
                "Packages/com.example.tool/package.json.meta",
                "Packages/com.example.tool/Runtime/Tool.cs",
                "Packages/com.example.tool/Runtime/Tool.cs.meta",
            })

    def test_packages_path_without_package_json_is_relocated(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, result = self._unpack(tmp, {
                "Packages/com.example.loose/Runtime/Loose.cs": b"class Loose {}",
                # a package.json the Package Manager cannot load does not count
                "Packages/com.example.broken/package.json": b"{ not json",
                "Packages/com.example.broken/Runtime/Broken.cs": b"class Broken {}",
                # neither does one below the package root
                "Packages/com.example.nested/Docs/package.json": UPM_MANIFEST,
            })
            self.assertEqual(result["embedded_packages"], [])
            sandbox = "Assets/_Quartermaster_Imported/Packages/"
            self.assertEqual(_tree(project), {
                "Packages/manifest.json",
                sandbox + "com.example.loose/Runtime/Loose.cs",
                sandbox + "com.example.loose/Runtime/Loose.cs.meta",
                sandbox + "com.example.broken/package.json",
                sandbox + "com.example.broken/package.json.meta",
                sandbox + "com.example.broken/Runtime/Broken.cs",
                sandbox + "com.example.broken/Runtime/Broken.cs.meta",
                sandbox + "com.example.nested/Docs/package.json",
                sandbox + "com.example.nested/Docs/package.json.meta",
            })

    def test_packages_traversal_attempts(self):
        for raw in ("Packages/com.example.tool/../manifest.json",
                    "Packages/com.example.tool/C:/Windows/evil.dll",
                    "C:/Packages/com.example.tool/Runtime/Tool.cs",
                    "Packages/com.example.tool/Runtime/Tool\x00.cs",
                    "Packages/com.example.tool"):
            self.assertIsNone(_embedded_package_path(raw), repr(raw))

        with tempfile.TemporaryDirectory() as tmp:
            for name, parts in (("com.example.tool", ["..", "manifest.json"]),
                                ("com.example.tool", []),
                                ("..", ["ProjectSettings", "ProjectSettings.asset"])):
                with self.assertRaises(ValueError):
                    _safe_package_target(tmp, name, parts)

            project, result = self._unpack(tmp, {
                "Packages/com.example.tool/package.json": UPM_MANIFEST,
                "Packages/com.example.tool/../manifest.json": b"{}",
                "Packages/com.example.tool/../../ProjectSettings/ProjectSettings.asset": b"evil",
                "Packages/../../../outside.txt": b"evil",
                # a package.json smuggled in through '..' does not make an embedded package
                "Packages/x/../com.example.sneaky/package.json": UPM_MANIFEST,
                "Packages/com.example.sneaky/Runtime/Sneaky.cs": b"evil",
                "C:/Packages/com.example.tool/Runtime/Drive.cs": b"evil",
                "Packages/com.example.tool/Runtime/Ctrl\x01.cs": b"evil",
            })
            self.assertEqual(result["embedded_packages"], ["com.example.tool"])
            self.assertEqual(result["written"], 8)
            # nothing outside the project, manifest untouched, everything else sandboxed
            self.assertEqual(sorted(os.listdir(tmp)), ["Project", "Test.unitypackage"])
            with open(os.path.join(project, "Packages", "manifest.json")) as f:
                self.assertEqual(f.read(), PROJECT_MANIFEST)
            self.assertEqual({p for p in _tree(project)
                              if not p.startswith("Assets/_Quartermaster_Imported/")}, {
                "Packages/manifest.json",
                "Packages/com.example.tool/package.json",
                "Packages/com.example.tool/package.json.meta",
            })


if __name__ == "__main__":
    unittest.main()
