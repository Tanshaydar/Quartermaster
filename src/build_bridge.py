"""
Builds editor_bridge/Quartermaster-Bridge.unitypackage from the C# editor window.

A .unitypackage is a gzipped tar where each file lives in its own GUID folder:
    <guid>/pathname   -> original project path (e.g. Assets/Editor/VaultMCP/QuartermasterWindow.cs)
    <guid>/asset      -> file bytes
    <guid>/asset.meta -> Unity .meta sidecar (we generate a minimal valid one)

This is the same format src/unpacker.py reads — dogfooding on purpose.

Unity matches package assets to project assets by GUID, so a shipped file must
keep its GUID forever: an update that brings QuartermasterWindow.cs under a new
GUID is imported as a second copy next to the old one, defining the class twice.
See asset_guid(). The output is also reproducible: same inputs, same bytes.
"""
import gzip
import io
import os
import tarfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "editor_bridge")
OUT = os.path.join(SRC_DIR, "Quartermaster-Bridge.unitypackage")

# Shipped in v1.0.0 with random GUIDs, which projects that imported it now hold.
# Never change these. To rename or move a shipped file, map its new path to its
# old GUID here, so existing copies get updated instead of duplicated.
PINNED_GUIDS = {
    "Assets/Editor/VaultMCP": "e4de3898e904461c8ded14e88fa217df",
    "Assets/Editor/VaultMCP/QuartermasterWindow.cs": "8d6f9bdb10f14aad9dd8f32141c897e4",
}

# Other paths get a GUID derived from their package path. Never change the
# namespace: that would re-GUID every file already shipped this way.
GUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Tanshaydar/Quartermaster/editor_bridge")

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


def asset_guid(package_path):
    return PINNED_GUIDS.get(package_path) or uuid.uuid5(GUID_NAMESPACE, package_path).hex


def build(src_dir=SRC_DIR, out_path=OUT):
    files = []
    for name in sorted(os.listdir(src_dir)):
        p = os.path.join(src_dir, name)
        if os.path.isfile(p) and name.endswith(".cs"):
            files.append(("Assets/Editor/VaultMCP/" + name, p))

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        # folder entries (Unity wants metas for folders too)
        folders = ["Assets/Editor/VaultMCP"]
        for folder in folders:
            guid = asset_guid(folder)
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
            guid = asset_guid(rel_path)
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

    # GzipFile, not tarfile's "w:gz" (stamps build time and file name into the
    # header) or gzip.compress() (lets zlib set the OS byte on some Pythons).
    # TarInfo's defaults (mtime 0, uid/gid 0) already keep the tar entries stable.
    with open(out_path, "wb") as f, gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
        gz.write(buf.getvalue())

    print(f"[ok] built {out_path} ({os.path.getsize(out_path)} bytes)")


if __name__ == "__main__":
    build()
