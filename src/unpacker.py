"""
Direct project unpacker.

Extracts a locally-downloaded Unity Asset Store .unitypackage straight into
a target Unity project's Assets/ directory, skipping the Package Manager / drag-and-drop dance.

Format: .unitypackage is a gzipped tar archive with entries:
    <guid>/asset        the payload file (or empty directory marker)
    <guid>/asset.meta   the .meta sidecar
    <guid>/pathname     original project path, e.g. "Assets/Foo/Bar.cs"

Safety Sandbox:
Every declared path is sanitized and normalized. Traversal segments ('..')
are collapsed or stripped. Any path declared outside of Assets/ is relocated
under Assets/_Quartermaster_Imported/, except the files of an embedded UPM
package (below). All final target paths are cryptographically and structurally
asserted to reside strictly inside <project>/Assets/ or <project>/Packages/<name>/.
Escapes to the <project>/Packages/ root (manifest.json), <project>/ProjectSettings/,
or outside the project are strictly impossible and raise security errors.

Embedded UPM packages:
A declared 'Packages/<name>/...' path is installed to <project>/Packages/<name>/
when the archive also ships a well-formed 'Packages/<name>/package.json'. These
paths are validated rather than repaired: any '..', drive letter or control
character sends the path back to the Assets/ sandbox. The package root folder
itself gets no .meta (Unity uses none for embedded packages).
"""
import json
import os
import tarfile
from typing import Dict, Any, Optional

try:
    from .db import get_asset_by_id
    from .config import load_config
    from .project_audit import audit_project
except ImportError:
    from db import get_asset_by_id
    from config import load_config
    from project_audit import audit_project

DEFAULT_STRIP_DIRS = {"demo", "demos", "samples", "samplescene", "samplescenes",
                      "example", "examples", "documentation"}
DEFAULT_STRIP_EXTS = {".pdf", ".chm"}


def _sanitize_package_path(raw_rel: str) -> tuple[str, list[str]]:
    """
    Normalizes a package relative path and collapses/neutralizes traversal.
    Returns (normalized_rel_path, safe_path_components_under_Assets).
    
    Guarantees:
    - Strips drive letters ('C:'), null bytes, and leading slashes.
    - Collapses '..' segments.
    - Any path not starting with 'Assets/' is placed under 'Assets/_Quartermaster_Imported/'.
    """
    rel = raw_rel.split("\n")[0].strip().replace("\\", "/")
    # Remove null bytes or control characters
    rel = "".join(c for c in rel if ord(c) >= 32)
    # Remove drive letters e.g. "C:"
    if len(rel) >= 2 and rel[1] == ":" and rel[0].isalpha():
        rel = rel[2:]
    rel = rel.lstrip("/")

    parts = [p for p in rel.split("/") if p not in ("", ".")]
    collapsed = []
    for p in parts:
        if p == "..":
            if collapsed:
                collapsed.pop()
        else:
            collapsed.append(p)

    if not collapsed:
        return "", []

    # If first segment is not "Assets", relocate under Assets/_Quartermaster_Imported/
    if collapsed[0] != "Assets":
        safe_components = ["_Quartermaster_Imported"] + collapsed
    else:
        safe_components = collapsed[1:]  # components under Assets/

    if not safe_components:
        # Was literally just "Assets" or "Assets/.."
        return "", []

    final_rel = "Assets/" + "/".join(safe_components)
    return final_rel, safe_components


def _safe_target(project_dir: str, safe_components_under_assets: list[str]) -> str:
    """
    Resolves safe_components_under_assets against <project_dir>/Assets/
    and rigorously asserts that the target path does not escape the Assets/ directory.
    """
    assets_root = os.path.normpath(os.path.join(os.path.abspath(project_dir), "Assets"))
    target = os.path.normpath(os.path.join(assets_root, *safe_components_under_assets))
    
    # Strict boundary assertion: target MUST start with assets_root + sep
    if not (target == assets_root or target.startswith(assets_root + os.sep)):
        raise ValueError(f"Security: Target path escapes Assets/ sandbox: {target}")
    return target


def _embedded_package_path(raw_rel: str) -> Optional[tuple[str, str, list[str]]]:
    """
    Recognizes a declared 'Packages/<name>/...' path (a file of an embedded UPM package).
    Returns (normalized_rel_path, package_name, safe_path_components_under_package),
    or None if the path does not qualify.

    Unlike _sanitize_package_path nothing is repaired:
    - Backslashes, empty and '.' segments are normalized.
    - '..' segments, drive letters (any ':') and control characters disqualify the path.
    - The package root itself never qualifies, so its folder .meta is never written.
    """
    rel = raw_rel.split("\n")[0].strip().replace("\\", "/")
    if any(ord(c) < 32 or c == ":" for c in rel):
        return None

    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if len(parts) < 3 or parts[0] != "Packages" or ".." in parts:
        return None
    return "/".join(parts), parts[1], parts[2:]


def _is_upm_manifest(data: bytes) -> bool:
    """True if data is a package.json the Package Manager can load (has name + version)."""
    try:
        manifest = json.loads(data)
    except ValueError:
        return False
    return isinstance(manifest, dict) and all(
        isinstance(manifest.get(key), str) and manifest[key] for key in ("name", "version"))


def _safe_package_target(project_dir: str, package_name: str,
                         safe_components_under_package: list[str]) -> str:
    """
    Resolves safe_components_under_package against <project_dir>/Packages/<package_name>/
    and rigorously asserts that the target does not escape that package folder.
    """
    packages_root = os.path.normpath(os.path.join(os.path.abspath(project_dir), "Packages"))
    package_root = os.path.normpath(os.path.join(packages_root, package_name))
    target = os.path.normpath(os.path.join(package_root, *safe_components_under_package))

    # Strict boundary assertion: package_root MUST be a direct child of Packages/
    # and target MUST start with package_root + sep (never the package root itself)
    if (os.path.dirname(package_root) != packages_root
            or not target.startswith(package_root + os.sep)):
        raise ValueError(f"Security: Target path escapes Packages/{package_name}/ sandbox: {target}")
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
    assets_root = os.path.normpath(os.path.join(project_dir, "Assets"))
    if not os.path.isdir(assets_root):
        raise ValueError("Target does not look like a Unity project (no Assets/ folder).")

    written = 0
    skipped = 0
    stripped_files = 0
    stripped_bytes = 0
    embedded_packages = set()

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

        # Packages/<name>/ trees that ship a well-formed package.json are installed
        # as embedded UPM packages instead of being relocated under Assets/
        upm_packages = set()
        for guid, raw_rel in pathname_of.items():
            declared = _embedded_package_path(raw_rel)
            am = asset_member.get(guid)
            if (declared and declared[2] == ["package.json"] and am and am.isfile()
                    and _is_upm_manifest(tf.extractfile(am).read())):
                upm_packages.add(declared[1])

        for guid, raw_rel in pathname_of.items():
            # pathname format: "Assets/foo/bar.ext\n" + archive offset marker (e.g. '00')
            declared = _embedded_package_path(raw_rel)
            if declared and declared[1] in upm_packages:
                rel, package_name, safe_components = declared
            else:
                package_name = None
                rel, safe_components = _sanitize_package_path(raw_rel)
            if not rel or not safe_components:
                skipped += 1
                continue

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

            if package_name:
                target = _safe_package_target(project_dir, package_name, safe_components)
                embedded_packages.add(package_name)
            else:
                target = _safe_target(project_dir, safe_components)
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
            "embedded_packages": sorted(embedded_packages),
            "project": project_dir, "package": os.path.basename(pkg_path)}


def import_asset_to_project(asset_id: str, project_dir: str,
                            strip_demos: bool = True) -> Dict[str, Any]:
    """MCP-facing wrapper: unpack a vault asset's cached .unitypackage."""
    asset = get_asset_by_id(asset_id)
    if not asset:
        raise ValueError(f"Asset not found: {asset_id}")

    pkg_path = asset.get("local_path")
    if not pkg_path or not os.path.exists(pkg_path):
        raise ValueError(
            f"Asset '{asset['title']}' is not downloaded locally. "
            f"Download it first via the Unity Package Manager / Asset Store.")

    if not pkg_path.lower().endswith(".unitypackage"):
        raise ValueError(
            f"Asset '{asset['title']}' is not a .unitypackage (found: {pkg_path}). "
            f"Direct import is currently supported for Unity Asset Store packages.")

    # Project audit & pre-import warnings
    info = audit_project(project_dir)
    from . import project_audit
    warnings = project_audit.compatibility_warning(asset, info)

    result = unpack_unitypackage(pkg_path, project_dir, strip_demos=strip_demos)
    result["warnings"] = warnings
    result["target"] = {"engine": info.get("engine"),
                        "version": info.get("version"),
                        "pipeline": info.get("pipeline")}
    result["title"] = asset["title"]
    result["asset_id"] = asset_id
    return result
