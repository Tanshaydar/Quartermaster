"""
VaultMCP ingestion engine.

Supported inputs:
  - Unity Asset Store CSV export (Package ID, Product ID, ... columns)
  - Fab / Unreal Marketplace CSV export (Listing ID, Asset Name, ...)
  - Generic JSON list of asset dicts

Usage:
  python -m src.ingest                                  # ingest all CSVs in data/seed/
  python -m src.ingest --csv path/to/file.csv           # ingest a specific CSV
  python -m src.ingest --csv file.csv --source unity    # force source type
"""
import csv
import json
import os
import re
import sys
from typing import Dict, Any, List, Optional

try:
    from .db import init_db, upsert_asset
    from .config import SEED_DIR
except ImportError:
    from db import init_db, upsert_asset
    from config import SEED_DIR


# ---------------------------------------------------------------------------
# Heuristic classifier (used until per-asset enrichment fills in real data)
# ---------------------------------------------------------------------------

def _word_match(text: str, keywords: List[str]) -> bool:
    """Checks if any keyword appears as a whole word or boundary-delimited phrase."""
    for kw in keywords:
        kw = kw.strip().lower()
        if not kw:
            continue
        if " " in kw or "-" in kw:
            if kw in text:
                return True
        else:
            rx = re.compile(r"\b" + re.escape(kw) + r"(?![a-z0-9])")
            if rx.search(text):
                return True
    return False


VISION_CONCEPT_MAP = {
    "Terrain & Landscape": [
        "rocky terrain", "mountain", "canyon", "desert", "cave", "landscape", "cliffs", "dunes",
        "desert dunes", "snowy mountains", "alien planet landscape", "waterfall river water", "ocean waves coastline"
    ],
    "3D Environments & Props": [
        "interior", "exterior", "building", "ruins", "dungeon", "architecture", "props", "furniture", "urban",
        "sci-fi corridor", "castle", "temple", "gothic architecture", "medieval village", "castle fortress",
        "ancient temple ruins", "cyberpunk city", "modern city street", "fantasy tavern interior", "dungeon prison",
        "abandoned industrial factory", "modular building kit pieces", "furniture interior props", "food and kitchen items",
        "medical laboratory equipment"
    ],
    "Foliage & Nature": [
        "forest", "jungle", "trees", "grass", "foliage", "plant", "nature", "vegetation", "dense forest", "jungle vegetation"
    ],
    "VFX & Particles": [
        "vfx", "fire", "smoke", "fluid", "explosion", "magic", "water splash", "electricity", "explosion fire effects",
        "magic spell effects"
    ],
    "3D Models & Characters": [
        "character", "creature", "humanoid", "monster", "animal", "human face", "robot", "cartoon style characters",
        "mech robot", "dinosaur monster creature", "humanoid character portrait"
    ],
    "Weapons & Combat": [
        "weapon", "gun", "sword", "firearm", "armor", "shield", "weapons display guns", "swords and medieval armor",
        "first person weapon viewmodel"
    ],
    "Vehicles": [
        "vehicle", "car", "aircraft", "ship", "truck", "spaceship vehicle", "racing car vehicle"
    ],
    "UI & Interface": [
        "ui", "hud", "game icons", "inventory interface", "game user interface menu"
    ],
    "Shaders & Rendering": [
        "stylized shader", "realistic shader", "pbr texture", "fog volume", "skybox", "hand painted stylized texture",
        "photorealistic render", "moody foggy scene", "stylized low poly environment", "voxel art", "pixel art"
    ],
}


def classify_asset(title: str, publisher: str = "", size_mb: float = 0.0,
                   tags_list: List[str] = None, vision_tags: Any = None, summary_text: str = "") -> Dict[str, Any]:
    t = title.lower()
    all_text = f"{t} {' '.join(tags_list or []).lower()} {(summary_text or '').lower()}"

    v_tags = []
    if vision_tags:
        if isinstance(vision_tags, list):
            v_tags = [str(v).strip().lower() for v in vision_tags]
        elif isinstance(vision_tags, str):
            v_str = vision_tags.strip()
            if v_str.startswith("["):
                try:
                    v_tags = [str(v).strip().lower() for v in json.loads(v_str)]
                except Exception:
                    v_tags = [v.strip().lower() for v in v_str.split(",") if v.strip()]
            else:
                v_tags = [v.strip().lower() for v in v_str.split(",") if v.strip()]

    category = "Tools & Utilities"
    pipelines = ["HDRP", "URP", "Built-in"]
    tags: List[str] = list(tags_list or [])
    summary = summary_text
    usage_notes = ""

    # Render pipeline detection (Unity-specific)
    if "hdrp" in t:
        pipelines = ["HDRP"]
        tags.append("hdrp")
    elif "urp" in t:
        pipelines = ["URP"]
        tags.append("urp")
    elif "built-in" in t or "birp" in t or "standard rendering" in t:
        pipelines = ["Built-in"]

    if _word_match(t, ["stampit", "heightmap", "heightmaps", "microsplat", "microverse", "terrain", "terrains",
                       "digger", "biome", "gaia", "infinitelands", "canyon", "canyons", "cliff", "cliffs",
                       "plateau", "plateaus", "landscape", "landscapes", "dune", "dunes", "mountain", "mountains",
                       "rock", "rocks", "rocky", "boulder", "boulders", "desert", "stone", "stones", "isles", "isle", "caves", "cave",
                       "dirt", "mud", "sand", "clay", "quarry", "soil"]):
        category = "Terrain & Landscape"
        tags.extend(["terrain", "heightmaps", "environment"])
        if not summary:
            summary = f"Terrain and landscape asset: {title}"

    elif _word_match(t, ["tree", "trees", "foliage", "vegetation", "grass", "forest", "forests", "plant",
                         "plants", "fern", "ivy", "meadow", "speedtree", "nature renderer", "megaplants",
                         "flower", "flowers", "bush", "bushes", "shrub", "shrubs", "wood", "woods", "flora",
                         "vines", "vine", "moss", "bamboo", "succulent", "succulents", "leaf", "leaves",
                         "palm", "aloe", "begonia", "cactus", "cacti", "bark", "trunk"]):
        category = "Foliage & Nature"
        tags.extend(["foliage", "vegetation", "nature", "flora"])
        if not summary:
            summary = f"Foliage / scanned nature library: {title}"

    elif _word_match(t, ["character", "characters", "creature", "creatures", "monster", "monsters",
                         "animal", "animals", "insect", "insects", "humanoid", "humanoids", "npc", "npcs",
                         "enemy", "enemies", "boss", "zombie", "zombies", "dinosaur", "dinosaurs", "dragon",
                         "dragons", "bird", "birds", "fish", "human", "people", "hero", "villain", "girl",
                         "guy", "man", "woman", "boy", "paragon", "warrior", "knight", "soldier", "avatar",
                         "skull", "skulls", "bone", "bones", "skeleton", "skeletons", "fossil", "fossils", "horn", "horns"]):
        category = "3D Models & Characters"
        tags.extend(["characters", "creatures", "3d-models"])
        if not summary:
            summary = f"3D character, creature, or anatomical model: {title}"

    elif _word_match(t, ["weapon", "weapons", "gun", "guns", "sword", "swords", "melee", "combat",
                         "fps", "shooter", "bow", "bows", "shield", "shields", "axe", "axes", "lmg", "smg",
                         "rifle", "rifles", "pistol", "pistols", "shotgun", "shotguns", "grenade", "dagger",
                         "knife", "knives", "cannon", "artillery", "armory", "ammo", "ammunition", "bullet", "bullets"]):
        category = "Weapons & Combat"
        tags.extend(["weapons", "combat", "gameplay"])
        if not summary:
            summary = f"Weapon / combat system or models: {title}"

    elif _word_match(t, ["vehicle", "vehicles", "car", "cars", "truck", "trucks", "arcade vehicle",
                         "wheel", "wheels", "boat", "boats", "ship", "ships", "plane", "planes",
                         "aircraft", "helicopter", "tank", "tanks", "automobile", "wagon", "train", "locomotive"]):
        category = "Vehicles"
        tags.extend(["vehicles", "physics", "driving"])
        if not summary:
            summary = f"Vehicle system or models: {title}"

    elif _word_match(t, ["vfx", "fluid", "fluids", "blood", "fire", "shockwave", "shockwaves", "lightning",
                         "water splash", "explosion", "explosions", "particle", "particles", "aura", "spell",
                         "spells", "laser", "lasers", "projectile", "projectiles", "magic effect", "fx",
                         "lens flare", "lens flares", "glow", "sparks", "water", "ocean", "river", "rivers", "lake", "lakes"]):
        category = "VFX & Particles"
        tags.extend(["vfx", "particles", "effects"])
        if not summary:
            summary = f"Visual effect / simulation system: {title}"

    elif _word_match(t, ["environment", "environments", "interior", "interiors", "exterior", "exteriors",
                         "cathedral", "castle", "temple", "dungeon", "dungeons", "village", "city", "warehouse",
                         "factory", "saloon", "hospital", "bunker", "street", "megapack", "modular", "abandoned",
                         "ruins", "ruin", "house", "building", "buildings", "props", "prop", "synty", "polygon",
                         "trench", "trenches", "corridor", "corridors", "room", "rooms", "kitchen", "restaurant",
                         "food", "furniture", "table", "chair", "tavern", "bar", "slums", "slum", "shanty",
                         "station", "cyberpunk", "sci-fi", "urban", "decay", "subway", "metro",
                         "concrete", "barrier", "barriers", "beam", "beams", "plank", "planks", "board", "boards",
                         "brick", "bricks", "block", "blocks", "column", "columns", "pillar", "pillars", "pipe", "pipes",
                         "metal", "corrugated", "cable", "cables", "wire", "wires", "crate", "crates", "barrel", "barrels",
                         "pallet", "pallets", "container", "containers", "scaffolding", "sandbag", "sandbags",
                         "debris", "rubble", "towel", "cloth", "supplies", "sign", "signs", "statue", "statues",
                         "stairs", "staircase", "wall", "walls", "floor", "floors", "ground", "grounds", "path", "paths",
                         "door", "doors", "window", "windows", "arch", "arches", "fence", "fences", "gate", "gates",
                         "tombstone", "slab", "bench", "lamp", "pole", "vent", "vents", "tank", "valve", "cart",
                         "wheelbarrow", "puddle", "puddles", "gravel", "asphalt", "cobblestone", "sidewalk", "curb",
                         "trash", "rubbish", "pile", "piles", "stack", "stacks", "structure", "decor"]):
        category = "3D Environments & Props"
        tags.extend(["3d-models", "environment", "modular", "props", "architecture"])
        if not summary:
            summary = f"Modular 3D environment kit or props: {title}"

    elif _word_match(t, ["shader", "shaders", "skybox", "skyboxes", "pixelize", "outline", "lut",
                         "htrace", "beautify", "upscaling", "dlss", "xess", "fsr", "fog volume",
                         "post processing", "lighting box", "material", "materials", "auto material",
                         "texture", "textures", "pbr", "decal", "decals", "surface", "surfaces", "track"]):
        category = "Shaders & Rendering"
        tags.extend(["shader", "rendering", "graphics"])
        if not summary:
            summary = f"Rendering enhancement / shader tool: {title}"

    elif _word_match(t, ["anim", "animation", "animations", "animator", "mocap", "locomotion",
                         "movement", "character controller", "climb", "parkour", "rig", "rigs", "rigging", "skeleton"]):
        category = "Animation & Rigging"
        tags.extend(["animation", "locomotion", "character"])
        if not summary:
            summary = f"Animation pack or character locomotion system: {title}"

    elif _word_match(t, ["sound", "sounds", "audio", "music", "sfx", "footstep", "footsteps", "speech",
                         "stinger", "ambience", "ambiance", "foley", "soundtrack", "voice"]):
        category = "Audio & SFX"
        tags.extend(["audio", "sound-effects", "foley", "music"])
        if not summary:
            summary = f"Audio asset collection: {title}"

    elif _word_match(t, ["ui", "gui", "hud", "menu", "menus", "dialogue", "inventory", "icon", "icons",
                         "loading screen", "scroller", "fantasy interface", "canvas", "crosshair"]):
        category = "UI & Interface"
        tags.extend(["ui", "gui", "hud", "interface", "canvas"])
        if not summary:
            summary = f"User interface system or graphics pack: {title}"

    elif _word_match(t, ["ai", "assistant", "whisper", "speech recognition", "dialogue system",
                         "flowcanvas", "nodecanvas", "playmaker", "behavior", "behavior tree", "state machine"]):
        category = "AI & Visual Scripting"
        tags.extend(["ai", "logic", "scripting", "behavior"])
        if not summary:
            summary = f"AI reasoning, behavior tree, or visual scripting framework: {title}"

    # 2. Vision Concept Inference from zero-shot CLIP tags
    elif v_tags:
        for cat, concepts in VISION_CONCEPT_MAP.items():
            if any(c in v_tags for c in concepts):
                category = cat
                break

    # 3. Store tags & summary keyword fallback
    elif _word_match(all_text, ["plants", "trees", "foliage", "vegetation", "grass", "vines"]):
        category = "Foliage & Nature"
    elif _word_match(all_text, ["props", "furniture", "architecture", "interior", "exterior", "modular building", "urban", "slums", "station"]):
        category = "3D Environments & Props"
    elif _word_match(all_text, ["guns", "weapons", "firearms", "swords", "ammo"]):
        category = "Weapons & Combat"
    elif _word_match(all_text, ["monsters", "characters", "creatures", "animals", "humanoid", "skull", "bones"]):
        category = "3D Models & Characters"
    elif _word_match(all_text, ["shaders", "materials", "textures", "surfaces"]):
        category = "Shaders & Rendering"
    elif _word_match(all_text, ["quixel", "megascans", "megaplants", "3d scan", "scanned", "photogrammetry"]):
        category = "3D Environments & Props"
        tags.extend(["3d-models", "props", "scanned"])
        if not summary:
            summary = f"Scanned 3D asset: {title}"

    # 4. Canonical tools check (inspect title for genuine tool keywords)
    elif _word_match(t, ["tool", "tools", "utility", "utilities", "editor", "inspector", "baker",
                         "generator", "importer", "exporter", "converter", "debugger", "profiler",
                         "package", "sdk", "framework", "system", "helper", "brush", "extension"]):
        category = "Tools & Utilities"

    else:
        category = "Tools & Utilities"
        tags.extend(["utility", "editor"])
        if not summary:
            summary = f"Developer utility / general asset pack: {title}"

    return {
        "category": category,
        "render_pipelines": pipelines,
        "tags": sorted(set(tags)),
        "summary": summary,
        "usage_notes": usage_notes,
    }


# ---------------------------------------------------------------------------
# CSV parsers
# ---------------------------------------------------------------------------

def _parse_size_mb(size_str: str) -> float:
    size_str = (size_str or "").strip().upper()
    m = re.search(r"([\d.,]+)", size_str)
    if not m:
        return 0.0
    val = float(m.group(1).replace(",", ""))
    if "GB" in size_str:
        return val * 1024
    if "KB" in size_str:
        return val / 1024
    return val


def detect_source(headers: List[str]) -> str:
    h = {c.strip().lower() for c in headers}
    if "package id" in h or "product id" in h or "item id" in h:
        return "unity"
    if "listing id" in h or "seller/publisher" in h or "acquired date" in h:
        return "fab"
    return "unknown"


def _stable_id(prefix: str, identifier: str, fallback_title: str) -> str:
    if identifier and str(identifier).strip():
        return f"{prefix}_{str(identifier).strip()}"
    import hashlib
    h = hashlib.sha1(fallback_title.lower().strip().encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{h}"

def _row_from_unity(row: Dict[str, str]) -> Optional[Dict[str, Any]]:
    title = (row.get("Asset Name") or "").strip()
    if not title:
        return None
    pkg_id = (row.get("Package ID") or "").strip()
    store_url = (row.get("Store URL") or "").strip()
    if not store_url and pkg_id and pkg_id.isdigit():
        store_url = f"https://assetstore.unity.com/packages/slug/{pkg_id}"
    elif "packages/slug-" in store_url:
        store_url = store_url.replace("packages/slug-", "packages/slug/")
    cls = classify_asset(title, row.get("Publisher", ""), _parse_size_mb(row.get("Size", "")))
    return {
        "id": _stable_id("unity", pkg_id, title),
        "source": "unity",
        "package_id": pkg_id,
        "product_id": (row.get("Product ID") or "").strip(),
        "title": title,
        "publisher": (row.get("Publisher") or "").strip(),
        "publisher_id": (row.get("Publisher ID") or "").strip(),
        "version": (row.get("Version") or "").strip(),
        "size_mb": round(_parse_size_mb(row.get("Size", "")), 2),
        "size_str": (row.get("Size") or "").strip(),
        "claimed_date": (row.get("Claimed/Grant Date") or "").strip(),
        "store_url": store_url,
        **cls,
        "image_url": "",
        "gallery_images": [],
        "video_links": [],
    }


def _row_from_fab(row: Dict[str, str]) -> Optional[Dict[str, Any]]:
    title = (row.get("Asset Name") or "").strip()
    if not title:
        return None
    listing_id = (row.get("Listing ID") or "").strip()
    store_url = (row.get("Store URL") or "").strip()
    if not store_url and listing_id:
        store_url = f"https://www.fab.com/listings/{listing_id}"
    formats = [f.strip() for f in (row.get("Formats") or "").split(",") if f.strip()]
    cls = classify_asset(title, row.get("Seller/Publisher", ""))
    cls["render_pipelines"] = []  # pipeline concept doesn't apply to Fab listings
    return {
        "id": _stable_id("fab", listing_id, title),
        "source": "fab",
        "package_id": listing_id,
        "product_id": "",
        "title": title,
        "publisher": (row.get("Seller/Publisher") or "").strip(),
        "publisher_id": "",
        "version": "",
        "size_mb": 0.0,
        "size_str": "",
        "claimed_date": (row.get("Acquired Date") or "").strip(),
        "store_url": store_url,
        "license": (row.get("License") or "").strip(),
        "formats": formats,
        **cls,
        "image_url": "",
        "gallery_images": [],
        "video_links": [],
    }


def _row_from_generic(row: Dict[str, str], source: str = "generic") -> Optional[Dict[str, Any]]:
    title = (row.get("Title") or row.get("Name") or row.get("Asset Name") or "").strip()
    if not title:
        return None
    pkg_id = (row.get("ID") or row.get("Package ID") or row.get("Listing ID") or row.get("Product ID") or "").strip()
    store_url = (row.get("Store URL") or row.get("URL") or row.get("Link") or "").strip()
    publisher = (row.get("Publisher") or row.get("Seller") or row.get("Creator") or row.get("Author") or "").strip()
    cls = classify_asset(title, publisher)
    return {
        "id": _stable_id(source, pkg_id, title),
        "source": source,
        "package_id": pkg_id,
        "product_id": "",
        "title": title,
        "publisher": publisher,
        "publisher_id": "",
        "version": (row.get("Version") or "").strip(),
        "size_mb": 0.0,
        "size_str": "",
        "claimed_date": (row.get("Date") or row.get("Claimed Date") or row.get("Acquired Date") or "")[:10],
        "store_url": store_url,
        **cls,
        "image_url": (row.get("Image") or row.get("Thumbnail") or "").strip(),
        "gallery_images": [],
        "video_links": [],
        "formats": [],
        "license": (row.get("License") or "").strip(),
        "enriched": 0,
    }


def ingest_csv(path: str, force_source: Optional[str] = None) -> int:
    """Ingest a single CSV export. Returns number of assets ingested."""
    count = 0
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        source = force_source or detect_source(headers)

        if source == "unknown":
            print(f"[skip] Unrecognized CSV format: {path}")
            return 0

        if source == "unity":
            parser = _row_from_unity
        elif source in ("fab", "quixel"):
            parser = _row_from_fab
        else:
            parser = lambda r: _row_from_generic(r, source=source)

        for row in reader:
            try:
                asset = parser(row)
            except Exception as e:
                print(f"[warn] Failed to parse row: {e}")
                continue
            if asset:
                upsert_asset(asset)
                count += 1
    print(f"[ok] Ingested {count} assets from {os.path.basename(path)} (source={source})")
    return count


def ingest_all_seeds() -> int:
    """Ingest every recognized CSV inside data/seed/."""
    init_db()
    total = 0
    if not os.path.isdir(SEED_DIR):
        print(f"No seed directory at {SEED_DIR}")
        return 0
    for name in sorted(os.listdir(SEED_DIR)):
        if name.lower().endswith(".csv"):
            total += ingest_csv(os.path.join(SEED_DIR, name))
    print(f"Done. Total ingested: {total}")
    return total


if __name__ == "__main__":
    args = sys.argv[1:]
    init_db()
    if "--csv" in args:
        i = args.index("--csv")
        path = args[i + 1]
        src = None
        if "--source" in args:
            src = args[args.index("--source") + 1]
        ingest_csv(path, src)
    else:
        ingest_all_seeds()
