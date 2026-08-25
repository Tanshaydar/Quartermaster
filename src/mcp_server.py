"""
VaultMCP MCP server (Model Context Protocol over stdio).

Register in an MCP client (Claude Desktop, Antigravity, pi, ...):
  command: python
  args:    ["-m", "src.mcp_server"]
  cwd:     <VaultMCP project root>

Tools:
  search_owned_assets(query, engine, pipeline, category, limit)
  get_asset_details(asset_id)
  list_asset_categories()
  get_stack_recommendations(problem_description)
  get_vault_stats()
"""
import json

try:
    from mcp.server.mcpserver import MCPServer as FastMCP      # mcp SDK >= 2.0
except ImportError:
    from mcp.server.fastmcp import FastMCP                     # mcp SDK 1.x

from .db import init_db, search_assets, get_asset_by_id, get_stats, DB_PATH

init_db()
from .ingest import classify_asset
from . import semantic, unpacker
from . import project_audit
from . import stack_rules

mcp = FastMCP("quartermaster")


def _slim(item: dict) -> dict:
    """Compact representation for LLM context efficiency."""
    out = {
        "id": item["id"],
        "engine": item.get("source"),
        "title": item["title"],
        "publisher": item.get("publisher"),
        "category": item.get("category"),
        "version": item.get("version"),
        "pipelines": item.get("render_pipelines") or item.get("formats"),
        "tags": item.get("tags"),
        "size": item.get("size_str") or None,
        "local": bool(item.get("local_path")),
        "local_path": item.get("local_path") or None,
        "summary": (item.get("summary") or "")[:220],
        "usage_notes": (item.get("usage_notes") or "")[:180],
        "store_url": item.get("store_url"),
    }
    # hybrid-search provenance (why this item matched)
    if item.get("match"):
        out["match"] = item["match"]
        out["relevance"] = item.get("relevance")
    return out


@mcp.tool()
def search_owned_assets(query: str, engine: str = "all", pipeline: str = "all",
                        category: str = "all", limit: int = 25,
                        local_only: bool = False) -> str:
    """Search the user's owned Unity/Fab asset library. Hybrid: keyword (BM25)
    fused with local semantic embeddings, so natural-language descriptions work
    even without exact keyword overlap. Results include a 'match' field showing
    why each item hit (keyword / semantic / both).
    engine: all|unity|fab. pipeline: all|HDRP|URP|Built-in.
    local_only: only assets already downloaded to disk."""
    if query.strip():
        merged = semantic.hybrid_search(query, limit=max(limit * 2, 50))
        results = merged.get("results", [])
        mode = merged.get("search_mode") or merged.get("mode") or "keyword"
        note = merged.get("note")
    else:
        results, mode = search_assets(limit=min(max(limit, 1), 100)), "keyword"
    if engine != "all":
        results = [r for r in results if r.get("source") == engine]
    if pipeline != "all":
        results = [r for r in results if pipeline in (r.get("render_pipelines") or [])]
    if category != "all":
        results = [r for r in results if r.get("category") == category]
    if local_only:
        results = [r for r in results if r.get("local_path")]
    out = {
        "count": len(results),
        "search_mode": mode,
        "results": [_slim(r) for r in results[:limit]],
    }
    if note:
        out["note"] = note
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def get_asset_details(asset_id: str) -> str:
    """Full details for one owned asset: summary, usage notes, gallery/video links, store URL."""
    item = get_asset_by_id(asset_id)
    if not item:
        return json.dumps({"error": f"No asset with id '{asset_id}'"})
    return json.dumps(item, ensure_ascii=False)


@mcp.tool()
def list_asset_categories() -> str:
    """List all categories and how many owned assets fall into each."""
    return json.dumps(get_stats(), ensure_ascii=False)


@mcp.tool()
def get_stack_recommendations(problem_description: str, limit_per_category: int = 5) -> str:
    """Given a game/feature description (e.g. 'third-person fantasy open world with
    magic combat and a village'), sweep the vault and recommend which owned packages
    fit which aspect of the problem."""
    text = problem_description.lower()
    words = set(__import__("re").findall(r"[a-z]{3,}", text))

    # Map problem vocabulary to vault categories
    aspect_map = {
        "Terrain & Landscape": ["terrain", "mountain", "canyon", "valley", "island", "open world",
                                "landscape", "cliff", "hill", "desert", "dune", "cave", "ground"],
        "3D Environments & Props": ["village", "city", "town", "castle", "dungeon", "house",
                                    "interior", "exterior", "building", "environment", "level",
                                    "street", "temple", "ruins", "warehouse"],
        "Foliage & Nature": ["forest", "tree", "jungle", "grass", "nature", "garden", "plant", "woods"],
        "VFX & Particles": ["magic", "spell", "fire", "explosion", "effect", "particle", "water",
                            "smoke", "lightning", "blood", "combat effect", "ability"],
        "Weapons & Combat": ["weapon", "gun", "sword", "melee", "combat", "shooter", "fps", "battle"],
        "Characters & Creatures": ["character", "creature", "enemy", "npc", "animal", "monster", "boss"],
        "Animation & Rigging": ["animation", "locomotion", "movement", "climb", "parkour", "controller"],
        "UI & Interface": ["ui", "hud", "menu", "inventory", "dialogue", "icon", "interface"],
        "Audio & SFX": ["sound", "music", "audio", "footstep", "ambience", "sfx"],
        "Shaders & Rendering": ["shader", "graphics", "lighting", "post processing", "stylized",
                                "realistic rendering", "fog", "outline"],
        "AI & Visual Scripting": ["ai", "behavior tree", "dialogue system", "npc logic", "state machine"],
        "Vehicles": ["vehicle", "car", "driving", "racing", "truck"],
    }

    # Also match creature/character keywords against titles directly
    scored_categories = []
    prob_lower = problem_description.lower()
    for cat, keywords in aspect_map.items():
        score = sum(1 for kw in keywords if (kw in prob_lower if " " in kw else kw in words))
        if score:
            scored_categories.append((cat, score))
    scored_categories.sort(key=lambda x: -x[1])

    recommendations: dict[str, list] = {}
    seen_cats = set()
    for cat, _score in scored_categories[:6]:
        hits = search_assets(query=None, category=cat, limit=limit_per_category)
        # rank within category: prefer title/tag keyword overlap
        def rel(h):
            hay = (h["title"] + " " + " ".join(h.get("tags", []))).lower()
            return sum(1 for w in words if w in hay)
        hits.sort(key=rel, reverse=True)
        recommendations[cat] = [_slim(h) for h in hits[:limit_per_category]]
        seen_cats.add(cat)

    return json.dumps({
        "problem": problem_description,
        "matched_aspects": [c for c, _ in scored_categories],
        "recommendations": recommendations,
        "note": "All recommended items are already owned by the user.",
    }, ensure_ascii=False)


@mcp.tool()
def audit_project(project_dir: str) -> str:
    """Detect the engine, version and render pipeline of a game project
    (Unity: from ProjectVersion.txt / manifest.json; Unreal: .uproject /
    DefaultEngine.ini). Call this BEFORE recommending or importing assets so
    pipeline mismatches (e.g. URP-only shader into an HDRP project) are caught."""
    try:
        info = project_audit.audit_project(project_dir)
        info.pop("packages", None)   # too noisy for agent context
        return json.dumps({"status": "ok", **info}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def import_asset_to_project(asset_id: str, project_dir: str) -> str:
    """Unpack a locally-downloaded Unity Asset Store package (.unitypackage)
    directly into a Unity project's Assets/ folder. Only works for assets
    tagged local=true (check with search first). project_dir must be the Unity
    project root (the folder containing Assets/)."""
    try:
        result = unpacker.import_asset_to_project(asset_id, project_dir)
        return json.dumps({"status": "ok", **result}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def validate_stack(asset_ids: list) -> str:
    """Lint a set of owned assets that are planned for one project/stack.
    Detects role conflicts (two weather systems, two vegetation renderers, …),
    same-product family notes, and missing prerequisites (e.g. MicroSplat module
    without core MicroSplat). Call before importing multiple assets together."""
    try:
        return json.dumps(stack_rules.validate_stack(asset_ids), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def list_stack_recipes() -> str:
    """Curated production stacks ('asset recipes') resolved against the user's
    OWN library. Each recipe lists which owned assets fill each slot."""
    try:
        return json.dumps(stack_rules.list_recipes(), ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool()
def get_vault_stats() -> str:
    """Overall vault stats: total assets per engine, per category, and how many
    are already downloaded locally vs cloud-only."""
    import sqlite3
    stats = get_stats()
    from .db import get_connection
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT source, COUNT(*) FROM assets WHERE local_path != '' GROUP BY source")
    local = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    stats["downloaded_locally"] = local
    return json.dumps(stats, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
