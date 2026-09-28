"""The Unity bridge package must keep the GUIDs v1.0.0 shipped with.

Unity matches package assets by GUID, so an update that brings
QuartermasterWindow.cs under a new GUID is imported as a second copy next to
the old one and the class is defined twice (CS0101).
"""
import contextlib
import io
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from collections import namedtuple
from unittest.mock import patch

from src import build_bridge

WINDOW = "Assets/Editor/VaultMCP/QuartermasterWindow.cs"

# Written out rather than read from build_bridge.PINNED_GUIDS: these are what
# users' projects already contain, whatever the build script says.
V1_GUIDS = {
    "Assets/Editor/VaultMCP": "e4de3898e904461c8ded14e88fa217df",
    WINDOW: "8d6f9bdb10f14aad9dd8f32141c897e4",
}

Entry = namedtuple("Entry", "guid asset meta")


def read_package(path):
    """Map each pathname in a .unitypackage to its Entry (asset is None for folders)."""
    with tarfile.open(path, "r:gz") as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    entries = {}
    for name, data in files.items():
        guid, _, leaf = name.partition("/")
        if leaf == "pathname":
            pathname = data.decode("utf-8").splitlines()[0]
            meta = files[guid + "/asset.meta"].decode("utf-8")
            entries[pathname] = Entry(guid, files.get(guid + "/asset"), meta)
    return entries


class TestBuildBridge(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def build(self, name="bridge.unitypackage", src_dir=build_bridge.SRC_DIR):
        out = os.path.join(self.tmp, name)
        with contextlib.redirect_stdout(io.StringIO()):
            build_bridge.build(src_dir=src_dir, out_path=out)
        return out

    def assert_v1_guids(self, entries):
        for pathname, guid in V1_GUIDS.items():
            self.assertEqual(entries[pathname].guid, guid, pathname)
            self.assertIn(f"\nguid: {guid}\n", entries[pathname].meta, pathname)

    def test_shipped_files_keep_v1_guids(self):
        self.assert_v1_guids(read_package(self.build()))

    def test_packaged_window_matches_source(self):
        with open(os.path.join(build_bridge.SRC_DIR, "QuartermasterWindow.cs"), "rb") as f:
            self.assertEqual(read_package(self.build())[WINDOW].asset, f.read())

    def test_rebuild_is_byte_identical(self):
        first = self.build("first.unitypackage")
        with patch("time.time", return_value=time.time() + 86400):
            second = self.build("second.unitypackage")
        with open(first, "rb") as a, open(second, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_new_file_gets_a_path_derived_guid(self):
        src = os.path.join(self.tmp, "src")
        os.mkdir(src)
        shutil.copy(os.path.join(build_bridge.SRC_DIR, "QuartermasterWindow.cs"), src)
        with open(os.path.join(src, "Extra.cs"), "w") as f:
            f.write("class Extra {}\n")

        entries = read_package(self.build(src_dir=src))
        self.assert_v1_guids(entries)
        # Hard-coded: once a derived GUID ships, changing the derivation would
        # re-GUID that file in users' projects.
        self.assertEqual(entries["Assets/Editor/VaultMCP/Extra.cs"].guid, "f2a3e63ff481538d9b7540a708ede21a")


if __name__ == "__main__":
    unittest.main()
