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

from .db import search_assets, get_asset_by_id, get_stats, DB_PATH
from .ingest import classify_asset

mcp = FastMCP("vaultmcp")


def _slim(item: dict) -> dict:
    """Compact representation for LLM context efficiency."""
    return {
        "id": item["id"],
        "engine": item.get("source"),
        "title": item["title"],
        "publisher": item.get("publisher"),
        "category": item.get("category"),
        "version": item.get("version"),
        "pipelines": item.get("render_pipelines") or item.get("formats"),
        "tags": item.get("tags"),
        "size": item.get("size_str") or None,
        "summary": (item.get("summary") or "")[:220],
        "usage_notes": (item.get("usage_notes") or "")[:180],
        "store_url": item.get("store_url"),
    }


@mcp.tool()
def search_owned_assets(query: str, engine: str = "all", pipeline: str = "all",
                        category: str = "all", limit: int = 25) -> str:
    """Full-text search across the user's owned Unity/Fab asset library.
    Use for questions like 'what do I own that could serve as X?'.
    engine: all|unity|fab. pipeline: all|HDRP|URP|Built-in."""
    results = search_assets(query=query, source=engine if engine != "all" else None,
                            pipeline=pipeline, category=category,
                            limit=min(max(limit, 1), 100))
    return json.dumps({
        "count": len(results),
        "results": [_slim(r) for r in results],
    }, ensure_ascii=False)


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
    for cat, keywords in aspect_map.items():
        score = len(words.intersection(set(kw.split()[0] if " " not in kw else kw.split()[0] for kw in keywords)))
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
def get_vault_stats() -> str:
    """Overall vault stats: total assets per engine, per category."""
    return json.dumps(get_stats(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
