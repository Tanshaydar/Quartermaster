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
except ImportError:
    from db import init_db, upsert_asset

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT_DIR, "data")
SEED_DIR = os.path.join(DATA_DIR, "seed")


# ---------------------------------------------------------------------------
# Heuristic classifier (used until per-asset enrichment fills in real data)
# ---------------------------------------------------------------------------

def classify_asset(title: str, publisher: str = "", size_mb: float = 0.0) -> Dict[str, Any]:
    t = title.lower()

    category = "Tools & Utilities"
    pipelines = ["HDRP", "URP", "Built-in"]
    tags: List[str] = []
    summary = ""
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

    if any(k in t for k in ["stampit", "heightmap", "microsplat", "microverse", "terrain", "digger",
                            "biome", "gaia", "infinitelands", "canyon", "cliff", "plateau",
                            "landscape", "dune", "mountain", "rock", "boulder", "desert", "stone"]):
        category = "Terrain & Landscape"
        tags.extend(["terrain", "heightmaps", "environment"])
        if "stampit" in t or "stamp" in t:
            tags.extend(["stamps", "procedural", "erosion"])
            summary = "Heightmap stamps and splatmaps for terrain sculpting in MicroVerse, Gaia, or Unity Terrain."
            usage_notes = "Use as non-destructive stamps in MicroVerse or Gaia. Pair with MicroSplat for procedural texturing."
        elif "microsplat" in t:
            tags.extend(["shader", "texturing", "blend", "triplanar"])
            summary = "Advanced modular terrain shading system supporting high splat counts, triplanar and procedural features."
            usage_notes = "Convert standard terrain shaders to MicroSplat. Core terrain pipeline foundation."
        elif "microverse" in t:
            tags.extend(["non-destructive", "stamps", "splines", "roads"])
            summary = "Non-destructive real-time terrain generator based on live stamps and modifiers."
            usage_notes = "Non-destructive authoring layer; adjust stamps without destructive baking."
        else:
            summary = f"Terrain and landscape asset: {title}"
            usage_notes = "Ideal for outdoor environment blockout and geological dressing."

    elif any(k in t for k in ["vfx", "fluid", "blood", "fire", "shockwave", "lightning", "water splash",
                              "explosion", "particle", "aura", "spell", "laser", "projectile", "magic effect"]):
        category = "VFX & Particles"
        tags.extend(["vfx", "particles", "effects"])
        if "water" in t or "ocean" in t or "river" in t:
            tags.extend(["water", "simulation"])
            summary = "Water/ocean simulation and shading system."
            usage_notes = "Configure reflection probes and planar reflections for high fidelity."
        elif "blood" in t:
            tags.extend(["combat", "gore", "decals"])
            summary = "Blood splashes, mist, and dynamic fluid FX for combat feedback."
        else:
            summary = f"Visual effect system: {title}"

    elif any(k in t for k in ["tree", "foliage", "vegetation", "grass", "forest", "plant", "fern",
                              "ivy", "meadow", "speedtree", "nature renderer"]):
        category = "Foliage & Nature"
        tags.extend(["foliage", "vegetation", "nature", "flora"])
        summary = f"Foliage / scanned nature library: {title}"
        usage_notes = "Check wind vertex displacement and subsurface settings for realism."

    elif any(k in t for k in ["sound", "audio", "music", "sfx", "footstep", "speech", "stinger",
                              "ambience", "ambiance", "foley"]):
        category = "Audio & SFX"
        tags.extend(["audio", "sound-effects", "foley", "music"])
        summary = f"Audio asset collection: {title}"
        usage_notes = "Import as WAV/Vorbis clips; key for diegetic atmosphere."

    elif any(k in t for k in ["ui", "gui", "hud", "menu", "dialogue", "inventory", "icon",
                              "loading screen", "scroller", "fantasy interface"]):
        category = "UI & Interface"
        tags.extend(["ui", "gui", "hud", "interface", "canvas"])
        summary = f"User interface system or graphics pack: {title}"
        usage_notes = "Adaptable to uGUI, TextMeshPro, UMG (Unreal) or UI Toolkit."

    elif any(k in t for k in ["anim", "mocap", "locomotion", "movement", "character controller",
                              "climb", "parkour", "humanoid", "rig", "skeleton"]):
        category = "Animation & Rigging"
        tags.extend(["animation", "locomotion", "character"])
        if "motion matching" in t:
            tags.extend(["next-gen", "procedural"])
            summary = "Motion matching animation controller for fluid character movement."
            usage_notes = "Requires pose trajectory databases; very fluid locomotion."
        else:
            summary = f"Animation pack or character locomotion system: {title}"

    elif any(k in t for k in ["shader", "pixelize", "outline", "lut", "htrace", "beautify",
                              "upscaling", "dlss", "xess", "fsr", "fog volume", "post processing",
                              "lighting box", "material"]):
        category = "Shaders & Rendering"
        tags.extend(["shader", "rendering", "graphics"])
        if "htrace" in t:
            tags.extend(["raytracing", "rtgi", "ambient-occlusion"])
            summary = "Ray-traced world-space global illumination and AO."
            usage_notes = "Calculates off-screen bounce and shadowing; great for canyon/valley geometry."
        elif "dlss" in t or "xess" in t or "fsr" in t:
            tags.extend(["upscaler", "super-resolution", "performance"])
            summary = "Temporal super-resolution upscaler integration."
        else:
            summary = f"Rendering enhancement / shader tool: {title}"

    elif any(k in t for k in ["environment", "interior", "exterior", "cathedral", "castle", "temple",
                              "dungeon", "village", "city", "warehouse", "factory", "saloon",
                              "hospital", "bunker", "street", "megapack", "modular", "abandoned",
                              "ruins", "house", "building"]):
        category = "3D Environments & Props"
        tags.extend(["3d-models", "environment", "modular", "props", "architecture"])
        summary = f"Modular 3D environment kit: {title}"
        usage_notes = "Extract period-neutral props or use modular pieces for level assembly."

    elif any(k in t for k in ["ai ", "assistant", "whisper", "speech recognition", "dialogue system",
                              "flowcanvas", "nodecanvas", "playmaker", "behavior", "state machine"]):
        category = "AI & Visual Scripting"
        tags.extend(["ai", "logic", "scripting", "behavior"])
        summary = f"AI reasoning, behavior tree, or visual scripting framework: {title}"
        usage_notes = "Enables node-based state machines, dialogue trees, or LLM-driven interactions."

    elif any(k in t for k in ["weapon", "gun", "sword", "melee", "combat", "fps", "shooter", "bow"]):
        category = "Weapons & Combat"
        tags.extend(["weapons", "combat", "gameplay"])
        summary = f"Weapon / combat system or models: {title}"

    elif any(k in t for k in ["vehicle", "car", "truck", "arcade vehicle", "wheel"]):
        category = "Vehicles"
        tags.extend(["vehicles", "physics", "driving"])
        summary = f"Vehicle system or models: {title}"

    else:
        category = "Tools & Utilities"
        tags.extend(["utility", "editor"])
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
        "store_url": (row.get("Store URL") or "").strip(),
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
        "store_url": (row.get("Store URL") or "").strip(),
        "license": (row.get("License") or "").strip(),
        "formats": formats,
        **cls,
        "image_url": "",
        "gallery_images": [],
        "video_links": [],
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

        parser = _row_from_unity if source == "unity" else _row_from_fab
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
