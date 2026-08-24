"""
Builds editor_bridge/Quartermaster-Bridge.unitypackage from the C# editor window.

A .unitypackage is a gzipped tar where each file lives in its own GUID folder:
    <guid>/pathname   -> original project path (e.g. Assets/Editor/VaultMCP/QuartermasterWindow.cs)
    <guid>/asset      -> file bytes
    <guid>/asset.meta -> Unity .meta sidecar (we generate a minimal valid one)

This is the same format src/unpacker.py reads — dogfooding on purpose.
"""
import io
import os
import tarfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "editor_bridge")
OUT = os.path.join(SRC_DIR, "Quartermaster-Bridge.unitypackage")

META_TEMPLATE = """fileFormatVersion: 2
guid: {guid}
{extra}"""

CS_META_EXTRA = """MonoImporter:
  externalObjects: {}
  serializedVersion: 2
  defaultReferences: []
  executionOrder: 0
  icon: {instanceID: 0}
  userData: 
  assetBundleName: 
  assetBundleVariant: """

FOLDER_META_EXTRA = """FolderImporter:
  externalObjects: {}"""


def build():
    files = []
    for name in sorted(os.listdir(SRC_DIR)):
        p = os.path.join(SRC_DIR, name)
        if os.path.isfile(p) and name.endswith(".cs"):
            files.append(("Assets/Editor/VaultMCP/" + name, p))

    with tarfile.open(OUT, "w:gz") as tar:
        # folder entries (Unity wants metas for folders too)
        folders = ["Assets/Editor/VaultMCP"]
        for folder in folders:
            guid = uuid.uuid4().hex
            meta = META_TEMPLATE.format(guid=guid, extra=FOLDER_META_EXTRA)
            entries = [
                (f"{guid}/pathname", (folder + "\n00").encode("utf-8")),
                (f"{guid}/asset.meta", meta.encode("utf-8")),
            ]
            for fname, data in entries:
                info = tarfile.TarInfo(name=fname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

        for rel_path, abs_path in files:
            guid = uuid.uuid4().hex
            meta = META_TEMPLATE.format(guid=guid, extra=CS_META_EXTRA)
            with open(abs_path, "rb") as f:
                asset_bytes = f.read()
            entries = [
                (f"{guid}/pathname", (rel_path + "\n00").encode("utf-8")),
                (f"{guid}/asset", asset_bytes),
                (f"{guid}/asset.meta", meta.encode("utf-8")),
            ]
            for fname, data in entries:
                info = tarfile.TarInfo(name=fname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

    print(f"[ok] built {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    build()
