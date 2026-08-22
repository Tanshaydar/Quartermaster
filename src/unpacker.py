"""
VaultMCP direct project unpacker.

Extracts a locally cached .unitypackage directly into a Unity project's
Assets/ directory, skipping the Package Manager / drag-and-drop dance.

.unitypackage format: a gzipped tar where each GUID folder contains
    <guid>/asset        the actual file bytes
    <guid>/asset.meta   the .meta sidecar
    <guid>/pathname     original project path, e.g. "Assets/Foo/Bar.cs"

Safety: paths are sanitized; anything outside Assets/ is relocated under
Assets/_VaultMCP_Imported/ ; nothing can escape the target project.
"""
import os
import tarfile
from typing import Any, Dict, Optional

try:
    from .db import get_asset_by_id
    from .config import load_config
except ImportError:
    from db import get_asset_by_id
    from config import load_config

# Default demo-content filters: any path segment matching these (case-insensitive)
# is skipped, plus documentation files.
DEFAULT_STRIP_DIRS = {"demo", "demos", "samples", "samplescene", "samplescenes",
                      "example", "examples", "documentation"}
DEFAULT_STRIP_EXTS = {".pdf", ".chm"}


def _safe_target(project_dir: str, rel_path: str) -> str:
    """Resolve rel under project_dir and refuse escapes."""
    target = os.path.normpath(os.path.join(project_dir, *rel_path.split("/")))
    root = os.path.normpath(project_dir)
    if not (target == root or target.startswith(root + os.sep)):
        raise ValueError(f"Unsafe path in package: {rel_path}")
    return target


def _strip_reason(rel_path: str, strip_dirs: set, strip_exts: set) -> Optional[str]:
    """Return a reason string if this relative path is demo/doc bloat."""
    parts = [p.lower() for p in rel_path.split("/")]
    for seg in parts[:-1]:
        if seg in strip_dirs:
            return f"dir:{seg}"
    ext = os.path.splitext(parts[-1])[1]
    if ext in strip_exts:
        return f"ext:{ext}"
    return None


def unpack_unitypackage(pkg_path: str, project_dir: str,
                        strip_demos: bool = True) -> Dict[str, Any]:
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise ValueError(f"Project dir does not exist: {project_dir}")
    # Heuristic sanity check: should look like a Unity project
    if not os.path.isdir(os.path.join(project_dir, "Assets")):
        raise ValueError("Target does not look like a Unity project (no Assets/ folder).")

    written = 0
    skipped = 0
    stripped_files = 0
    stripped_bytes = 0

    cfg = load_config()
    strip_dirs = set(cfg.get("strip_dirs", DEFAULT_STRIP_DIRS))
    strip_exts = set(cfg.get("strip_exts", DEFAULT_STRIP_EXTS))

    with tarfile.open(pkg_path, "r:gz") as tf:
        members = tf.getmembers()

        # pass 1: guid -> declared pathname
        pathname_of: Dict[str, str] = {}
        for m in members:
            parts = m.name.split("/", 1)
            if len(parts) == 2 and parts[1] == "pathname":
                f = tf.extractfile(m)
                if f:
                    pathname_of[parts[0]] = f.read().decode("utf-8", "ignore").strip()

        # pass 2: write asset + asset.meta to their declared locations
        asset_member: Dict[str, tarfile.TarInfo] = {}
        meta_member: Dict[str, tarfile.TarInfo] = {}
        for m in members:
            parts = m.name.split("/", 1)
            if len(parts) != 2:
                continue
            guid, rest = parts
            if rest == "asset":
                asset_member[guid] = m
            elif rest == "asset.meta":
                meta_member[guid] = m

        for guid, raw_rel in pathname_of.items():
            # pathname format: "Assets/foo/bar.ext\n" + archive offset marker (e.g. '00')
            rel = raw_rel.split("\n")[0].strip().replace("\\", "/").lstrip("/")
            if not rel:
                skipped += 1
                continue
            if not rel.startswith("Assets/"):
                rel = f"Assets/_VaultMCP_Imported/{rel}"

            am = asset_member.get(guid)
            if not am or not am.isfile():
                skipped += 1
                continue

            if strip_demos:
                reason = _strip_reason(rel, strip_dirs, strip_exts)
                if reason:
                    stripped_files += 1
                    stripped_bytes += am.size
                    continue

            target = _safe_target(project_dir, rel)

            am = asset_member.get(guid)
            if not am or not am.isfile():
                skipped += 1
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)

            src = tf.extractfile(am)
            with open(target, "wb") as out:
                out.write(src.read())
            written += 1

            mm = meta_member.get(guid)
            if mm:
                msrc = tf.extractfile(mm)
                with open(target + ".meta", "wb") as out:
                    out.write(msrc.read())

    return {"written": written, "skipped": skipped,
            "stripped": stripped_files,
            "stripped_mb": round(stripped_bytes / 1024 / 1024, 1),
            "project": project_dir, "package": os.path.basename(pkg_path)}


def import_asset_to_project(asset_id: str, project_dir: str,
                            strip_demos: bool = True) -> Dict[str, Any]:
    """MCP-facing wrapper: unpack a vault asset's cached .unitypackage."""
    asset = get_asset_by_id(asset_id)
    if not asset:
        raise ValueError(f"No asset with id '{asset_id}'")
    pkg = asset.get("local_path") or ""
    if not pkg.lower().endswith(".unitypackage") or not os.path.isfile(pkg):
        raise ValueError(
            f"'{asset['title']}' is not downloaded locally (no .unitypackage on disk). "
            "Download it via Unity Hub / the Asset Store first, then re-run the disk scan.")
    result = unpack_unitypackage(pkg, project_dir, strip_demos=strip_demos)
    result["title"] = asset["title"]
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3:
        print("Usage: python -m src.unpacker <asset_id> <project_dir>")
    else:
        print(import_asset_to_project(sys.argv[1], sys.argv[2]))
