"""
VaultMCP local disk-cache scanner.

Detects which owned assets are ALREADY DOWNLOADED on this machine and tags
them  local ("⚡ ready to import") vs cloud ("☁ needs download").

Scanned locations
-----------------
Unity : %APPDATA%/Unity/Asset Store-5.x/<Publisher>/<Store Category>/<Title>.unitypackage
Fab   : <VaultCacheDirectories>/FabLibrary/<slug>-<hash>/
        The vault root is auto-detected from the Epic Games Launcher config
        (GameUserSettings.ini -> VaultCacheDirectories), with a manual
        override possible via config.json -> "fab_vault_dirs".

Matching is done on normalized titles (case/punctuation-insensitive); the
Unity cache folders carry no package IDs, so title matching is the only
portable option.
"""
import glob
import os
import re
from typing import Any, Dict, List

try:
    from .db import get_connection, search_assets, DB_PATH, upsert_asset
    from .config import load_config
    from .ingest import classify_asset
except ImportError:
    from db import get_connection, search_assets, DB_PATH, upsert_asset
    from config import load_config
    from ingest import classify_asset

EPIC_LAUNCHER_INI = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "EpicGamesLauncher", "Saved",
    "Config", "WindowsEditor", "GameUserSettings.ini")


def _norm(title: str) -> str:
    """Normalize a title for fuzzy disk matching."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _detect_fab_vault_dirs(cfg: dict) -> List[str]:
    # explicit config wins
    if cfg.get("fab_vault_dirs"):
        d = cfg["fab_vault_dirs"]
        return d if isinstance(d, list) else [d]
    dirs: List[str] = []
    # auto-detect from Epic Games Launcher settings
    if os.path.exists(EPIC_LAUNCHER_INI):
        try:
            with open(EPIC_LAUNCHER_INI, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = re.match(r"\s*VaultCacheDirectories=(.+)", line)
                    if m:
                        for raw in m.group(1).split(";"):
                            p = raw.strip().rstrip("/\\")
                            if p:
                                dirs.append(os.path.join(p, "FabLibrary"))
        except Exception:
            pass
    # default location
    dirs.append(r"C:\ProgramData\Epic\EpicGamesLauncher\Data\VaultCache\FabLibrary")
    return [d for d in dirs if os.path.isdir(d)]


def scan_all(db_path: str = DB_PATH) -> Dict[str, Any]:
    cfg = load_config()

    found: Dict[str, str] = {}          # norm_title -> disk path
    unmatched: List[str] = []

    # 1. Discover all disk paths safely first BEFORE touching the database

    # ---------------- Unity ----------------
    unity_root = os.path.join(os.environ.get("APPDATA", ""), "Unity", "Asset Store-5.x")
    unity_count = 0
    if os.path.isdir(unity_root):
        for pkg_file in glob.glob(os.path.join(unity_root, "*", "*", "*.unitypackage")):
            title = os.path.splitext(os.path.basename(pkg_file))[0]
            key = _norm(title)
            found.setdefault(key, pkg_file)
            unity_count += 1

    # ---------------- Fab ----------------
    fab_count = 0
    for vault_dir in _detect_fab_vault_dirs(cfg):
        try:
            entries = os.listdir(vault_dir)
        except OSError as e:
            continue
        for entry in entries:
            path = os.path.join(vault_dir, entry)
            if not os.path.isdir(path):
                continue
            # entry format: <Slug_Name>-<8 hex chars>
            slug = re.sub(r"-[0-9a-f]{8}$", "", entry)
            title = slug.replace("_", " ").replace("-", " ")
            key = _norm(title)
            found.setdefault(key, path)
            fab_count += 1

    # ---------------- match against DB ----------------
    conn = get_connection(db_path)
    cur = conn.cursor()

    # Reset previous scan marks inside transaction only after disk read succeeds
    cur.execute("UPDATE assets SET local_path = '' WHERE source IN ('unity', 'fab')")

    cur.execute("SELECT id, title FROM assets")
    db_norms = {_norm(t): aid for aid, t in cur.fetchall()}
    matched = 0
    to_adopt = []
    for key, p in found.items():
        asset_id = db_norms.get(key)
        if asset_id:
            cur.execute("UPDATE assets SET local_path = ? WHERE id = ?",
                        (p, asset_id))
            matched += 1
        else:
            # Discovered on disk but missing from library imports -> adopt it
            if p.endswith(".unitypackage"):
                title = os.path.splitext(os.path.basename(p))[0]
                source = "unity"
                publisher = os.path.basename(os.path.dirname(os.path.dirname(p)))
            else:
                title = re.sub(r"-[0-9a-f]{8}$", "", os.path.basename(p)).replace("_", " ").replace("-", " ")
                source = "fab"
                publisher = ""
            to_adopt.append((key, p, title, source, publisher))

    conn.commit()  # release write lock before adopting via second connection

    adopted = 0
    for key, p, title, source, publisher in to_adopt:
        cls = classify_asset(title, publisher)
        import hashlib
        key_hash = hashlib.sha1(key.lower().strip().encode("utf-8")).hexdigest()[:16]
        new_id = f"{source}_disk_{key_hash}"
        upsert_asset({
            "id": new_id, "source": source, "package_id": "", "title": title,
            "publisher": publisher, "local_path": p,
            "store_url": "", "image_url": "",
            "gallery_images": [], "video_links": [],
            **cls,
        }, db_path)
        adopted += 1

    cur.execute(
        "SELECT source, COUNT(*) FROM assets WHERE local_path != '' GROUP BY source")
    local_by_source = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    result = {
        "files_scanned": {"unity": unity_count, "fab": fab_count},
        "matched_to_library": matched,
        "adopted_from_disk": adopted,
        "local_by_source": local_by_source,
    }
    print(f"[scan] {result}")
    return result


if __name__ == "__main__":
    scan_all()
