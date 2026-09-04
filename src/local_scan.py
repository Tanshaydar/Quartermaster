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
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

try:
    from .db import get_connection, search_assets, DB_PATH, upsert_asset, _sync_fts
    from .config import load_config
    from .ingest import classify_asset
except ImportError:
    from db import get_connection, search_assets, DB_PATH, upsert_asset, _sync_fts
    from config import load_config
    from ingest import classify_asset

EPIC_LAUNCHER_INI = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "EpicGamesLauncher", "Saved",
    "Config", "WindowsEditor", "GameUserSettings.ini")


def _norm(title: str) -> str:
    """Normalize a title for fuzzy disk matching."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


VALID_MAPS = {
    "basecolor", "albedo", "diffuse", "normal", "roughness", "gloss", "specular",
    "metallic", "metalness", "displacement", "height", "ao", "cavity", "opacity",
    "alpha", "translucency", "transmission", "bump", "curvature", "fuzz", "mask", "thickness"
}


def _parse_scan_specs_from_text(text: str) -> Dict[str, Any]:
    """Extract texel density, scan area / physical size, and map lists from listing HTML/markdown."""
    res = {}
    if not text:
        return res
    m_td = re.search(r"Texel\s*density:?\s*(?:</strong>)?\s*([0-9]+\s*px/m)", text, re.I)
    if m_td:
        res["texel_density"] = m_td.group(1).strip()
    m_sa = re.search(r"(?:Scan\s*Area|Physical\s*size):?\s*(?:</strong>)?\s*([0-9xX\.\s\w]+?)(?:</p>|<br|[\n\r]|$)", text, re.I)
    if m_sa and re.search(r"[0-9]", m_sa.group(1)):
        res["scan_area"] = m_sa.group(1).strip()
    m_maps = re.search(r"Maps:?\s*(?:</strong>)?\s*(.+?)(?:</p>|<br|[\n\r]|$)", text, re.I)
    if m_maps:
        clean_m = re.sub(r"<[^>]+>", " ", m_maps.group(1))
        clean_m = re.sub(r"\([^)]*\)", " ", clean_m)
        tokens = [w.strip() for w in re.split(r"[\s,]+", clean_m) if w.strip()]
        clean_maps = []
        for t in tokens:
            t_clean = re.sub(r"[^a-zA-Z0-9_\-]", "", t)
            if t_clean.lower() in VALID_MAPS and t_clean.title() not in clean_maps:
                clean_maps.append(t_clean.title())
        if clean_maps:
            res["maps"] = clean_maps
    return res


def _parse_scan_specs_from_dir(dir_path: str) -> Dict[str, Any]:
    """Inspect downloaded Quixel folder for internal metadata JSONs (maps, displacement scale, etc.)."""
    res = {}
    for root, dirs, files in os.walk(dir_path):
        for f in files:
            if f.endswith(".json") and not f.startswith("package"):
                fp = os.path.join(root, f)
                try:
                    j = json.load(open(fp, "r", encoding="utf-8"))
                    if "maps" in j and isinstance(j["maps"], list):
                        raw_maps = [m.get("name") or m.get("type") for m in j["maps"] if m.get("name") or m.get("type")]
                        seen = set()
                        unique_maps = []
                        for m in raw_maps:
                            cap = m.capitalize()
                            if cap not in seen:
                                seen.add(cap)
                                unique_maps.append(cap)
                        if unique_maps:
                            res["maps"] = unique_maps
                        if j["maps"] and j["maps"][0].get("physicalSize"):
                            ps = str(j["maps"][0]["physicalSize"]).strip()
                            if not ps.endswith("m"):
                                ps = f"{ps} m"
                            res["scan_area"] = ps
                    if "displacementScale" in j and j["displacementScale"] is not None:
                        res["displacement_scale"] = round(float(j["displacementScale"]), 4)
                    if "highest_available_res" in j and j["highest_available_res"]:
                        res["max_res"] = f"{j['highest_available_res']}px"
                except Exception:
                    pass
    return res


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
    unity_candidates = [
        # Windows
        os.path.join(os.environ.get("APPDATA", ""), "Unity", "Asset Store-5.x"),
        # Linux (XDG standard)
        os.path.expanduser("~/.local/share/unity3d/Asset Store-5.x"),
        # macOS
        os.path.expanduser("~/Library/Unity/Asset Store-5.x"),
    ]
    unity_found = {}
    unity_count = 0
    for unity_root in unity_candidates:
        if unity_root and os.path.isdir(unity_root):
            for pkg_file in glob.glob(os.path.join(unity_root, "*", "*", "*.unitypackage")):
                title = os.path.splitext(os.path.basename(pkg_file))[0]
                key = _norm(title)
                unity_found.setdefault(key, pkg_file)
                unity_count += 1

    # ---------------- Fab ----------------
    fab_found = []
    fab_count = 0
    catalog_specs = {}
    for vault_dir in _detect_fab_vault_dirs(cfg):
        # Preload catalog specs from internal listings_v1.db if present
        listings_db = os.path.join(vault_dir, "listings_v1.db")
        if os.path.exists(listings_db):
            try:
                lconn = sqlite3.connect(listings_db)
                lcur = lconn.cursor()
                lcur.execute("SELECT listing_uid, title, description FROM catalog WHERE description != ''")
                for luid, ltitle, ldesc in lcur.fetchall():
                    sp = _parse_scan_specs_from_text(ldesc)
                    if sp:
                        catalog_specs[luid.lower()] = sp
                lconn.close()
            except Exception:
                pass

        try:
            entries = os.listdir(vault_dir)
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(vault_dir, entry)
            if not os.path.isdir(path):
                continue
            # entry format: <Slug_Name>-<8 hex chars>
            m = re.search(r"-([0-9a-fA-F]{8})$", entry)
            hash_prefix = m.group(1).lower() if m else ""
            slug = re.sub(r"-[0-9a-fA-F]{8}$", "", entry)
            title = slug.replace("_", " ").replace("-", " ")
            key = _norm(title)
            fab_found.append({
                "path": path,
                "entry": entry,
                "hash_prefix": hash_prefix,
                "title": title,
                "key": key,
                "vault_dir": vault_dir
            })
            fab_count += 1

    # ---------------- match against DB ----------------
    conn = get_connection(db_path)
    cur = conn.cursor()

    # 1. Purge any legacy _disk_ stubs whose title already exists as a real store record
    cur.execute("""
        DELETE FROM assets 
        WHERE id LIKE '%_disk_%' 
          AND title IN (
              SELECT title FROM assets WHERE id NOT LIKE '%_disk_%'
          )
    """)

    # Reset previous scan marks inside transaction only after disk read succeeds
    cur.execute("UPDATE assets SET local_path = '' WHERE source IN ('unity', 'fab', 'quixel')")

    # Prioritize matching real library assets over disk stubs
    cur.execute("SELECT id, title, source FROM assets WHERE id NOT LIKE '%_disk_%'")
    real_db_norms = {_norm(t): (aid, src) for aid, t, src in cur.fetchall()}

    cur.execute("SELECT id, title, source FROM assets WHERE id LIKE '%_disk_%'")
    disk_db_norms = {_norm(t): (aid, src) for aid, t, src in cur.fetchall()}

    matched = 0
    to_adopt = []

    # 1. Match Fab folders by exact listing UUID hash prefix first, then fallback to title
    for item in fab_found:
        p = item["path"]
        key = item["key"]
        hash_prefix = item["hash_prefix"]
        aid = None
        apkg = ""

        if hash_prefix:
            cur.execute("""
                SELECT id, title, source, package_id, summary, usage_notes, tags, formats 
                FROM assets 
                WHERE id LIKE ? OR id LIKE ? OR package_id LIKE ?
            """, (f"quixel_{hash_prefix}%", f"fab_{hash_prefix}%", f"{hash_prefix}%"))
            rows = cur.fetchall()
            if rows:
                row = rows[0] if len(rows) == 1 else next((r for r in rows if _norm(r[1]) == key), rows[0])
                aid = row[0]
                apkg = row[3] or ""

        if not aid and key in real_db_norms:
            aid, _ = real_db_norms[key]
            apkg = ""

        if aid:
            # Extract scan specs from catalog_specs or internal folder JSONs
            specs = {}
            if apkg and apkg.lower() in catalog_specs:
                specs.update(catalog_specs[apkg.lower()])
            elif hash_prefix:
                for luid, sp in catalog_specs.items():
                    if luid.startswith(hash_prefix):
                        specs.update(sp)
                        break
            dir_specs = _parse_scan_specs_from_dir(p)
            specs.update({k: v for k, v in dir_specs.items() if v})

            cur.execute("SELECT summary, usage_notes, tags, formats, publisher FROM assets WHERE id = ?", (aid,))
            arow = cur.fetchone()
            if arow:
                old_sum, old_use, old_tags_json, old_fmt_json, pub = arow
                try:
                    curr_tags = json.loads(old_tags_json or "[]")
                except Exception:
                    curr_tags = []
                try:
                    curr_fmts = json.loads(old_fmt_json or "[]") if isinstance(old_fmt_json, str) else (old_fmt_json or [])
                except Exception:
                    curr_fmts = []

                notes_parts = []
                if specs.get("texel_density"):
                    notes_parts.append(f"Texel density: {specs['texel_density']}")
                if specs.get("scan_area"):
                    notes_parts.append(f"Scan area: {specs['scan_area']}")
                if specs.get("maps"):
                    notes_parts.append(f"Maps: {', '.join(specs['maps'])}")
                    for map_name in specs["maps"]:
                        ml = map_name.lower()
                        if ml in ("displacement", "roughness", "normal", "cavity", "ao", "specular") and ml not in curr_tags:
                            curr_tags.append(ml)
                if specs.get("displacement_scale"):
                    notes_parts.append(f"Displacement scale: {specs['displacement_scale']}")

                new_use = old_use or ""
                if notes_parts:
                    spec_note = " · ".join(notes_parts)
                    if "Texel density:" not in new_use:
                        new_use = f"{spec_note}\n{new_use}".strip() if new_use else spec_note

                new_sum = old_sum or ""
                spec_details = []
                if specs.get("scan_area"):
                    spec_details.append(specs["scan_area"])
                if specs.get("texel_density"):
                    spec_details.append(specs["texel_density"])
                if spec_details:
                    new_sum = re.sub(r"\s*\((?:[^)]*px/m|[^)]*\d+x\d+\s*m)[^)]*\)$", "", new_sum)
                    spec_str = f"({', '.join(spec_details)})"
                    new_sum = f"{new_sum} {spec_str}".strip()

                cur.execute("""
                    UPDATE assets 
                    SET local_path = ?, usage_notes = ?, summary = ?, tags = ?, formats = ? 
                    WHERE id = ?
                """, (p, new_use, new_sum, json.dumps(curr_tags), json.dumps(curr_fmts), aid))
                _sync_fts(cur, aid)
                matched += 1
                if key in disk_db_norms:
                    stub_id, _ = disk_db_norms[key]
                    cur.execute("DELETE FROM assets WHERE id = ?", (stub_id,))
        elif key in disk_db_norms:
            stub_id, _ = disk_db_norms[key]
            cur.execute("UPDATE assets SET local_path = ? WHERE id = ?", (p, stub_id))
            matched += 1
        else:
            to_adopt.append((key, p, item["title"], "fab", ""))

    # 2. Match Unity packages by title
    for key, p in unity_found.items():
        if key in real_db_norms:
            aid, _ = real_db_norms[key]
            cur.execute("UPDATE assets SET local_path = ? WHERE id = ?", (p, aid))
            matched += 1
            if key in disk_db_norms:
                stub_id, _ = disk_db_norms[key]
                cur.execute("DELETE FROM assets WHERE id = ?", (stub_id,))
        elif key in disk_db_norms:
            stub_id, _ = disk_db_norms[key]
            cur.execute("UPDATE assets SET local_path = ? WHERE id = ?", (p, stub_id))
            matched += 1
        else:
            title = os.path.splitext(os.path.basename(p))[0]
            publisher = os.path.basename(os.path.dirname(os.path.dirname(p)))
            to_adopt.append((key, p, title, "unity", publisher))

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
