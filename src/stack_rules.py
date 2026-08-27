"""
VaultMCP stack intelligence: role detection, conflict linting, and curated
"asset recipes" (complete production stacks) resolved against your own library.

Rules live in data/recipes.json — user-editable, no code changes needed.

MCP tools:
  validate_stack(asset_ids)      -> conflicts / missing prerequisites report
  list_stack_recipes()           -> curated stacks resolved to owned assets
"""
import json
import os
import re
from typing import Any, Dict, List

from typing import Any, Dict, List, Optional

try:
    from .db import get_connection, search_assets, DB_PATH
    from .config import RECIPES_PATH
except ImportError:
    from db import get_connection, search_assets, DB_PATH
    from config import RECIPES_PATH


def load_rules() -> Dict[str, Any]:
    if not os.path.exists(RECIPES_PATH):
        return {"roles": {}, "recipes": []}
    try:
        with open(RECIPES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] Failed to load recipes from {RECIPES_PATH}: {e}", file=sys.stderr)
        return {"roles": {}, "recipes": []}


def _title_matches(title: str, pattern: str) -> bool:
    """pattern: lowercase substring or 'a|b' alternatives.
    Matching is word-boundary aware so 'enviro' doesn't hit 'environment':
    each alternative must appear as a whole word (not followed by more letters)."""
    t = title.lower()
    for alt in pattern.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        rx = re.compile(r"\b" + re.escape(alt) + r"(?![a-z])")
        if rx.search(t):
            return True
    return False


def detect_roles_with_patterns(title: str, rules: Dict[str, Any]) -> List[tuple]:
    """Returns [(role_key, matched_pattern), ...]."""
    matches = []
    for role_key, spec in rules.get("roles", {}).items():
        for pat in spec.get("match", []):
            if _title_matches(title, pat):
                matches.append((role_key, pat))
                break
    return matches


def detect_roles(title: str, rules: Dict[str, Any]) -> List[str]:
    return [r[0] for r in detect_roles_with_patterns(title, rules)]


def validate_stack(asset_ids: List[str], db_path: str = DB_PATH) -> Dict[str, Any]:
    rules = load_rules()
    conn = get_connection(db_path)
    qmarks = ",".join("?" * len(asset_ids))
    rows = conn.execute(
        f"SELECT id, title, source, render_pipelines FROM assets WHERE id IN ({qmarks})",
        asset_ids).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    found_ids = {r["id"] for r in items}
    missing_ids = [a for a in asset_ids if a not in found_ids]

    # ---- role map ----
    role_map: Dict[str, List[tuple]] = {}
    for item in items:
        for role_key, pat in detect_roles_with_patterns(item["title"], rules):
            role_map.setdefault(role_key, []).append((item, pat))

    # ---- conflicts: exclusive roles filled twice+ ----
    conflicts = []
    family_notes = []
    for role_key, member_tuples in sorted(role_map.items()):
        spec = rules["roles"][role_key]
        members = [m[0] for m in member_tuples]
        patterns = [m[1] for m in member_tuples]
        if spec.get("exclusive") and len(members) > 1:
            entry = {
                "role": spec["label"],
                "assets": [{"id": m["id"], "title": m["title"]} for m in members],
                "advice": f"Pick one {spec['label'].lower()} per project; "
                          f"these systems will fight over the same subsystems.",
            }
            if len(set(patterns)) == 1:
                # all members matched the same rule pattern -> same brand/family
                entry.pop("advice")
                entry["note"] = ("These look like modules/versions of the same "
                                 "product family. Verify which variant you actually need "
                                 "before importing several.")
                family_notes.append(entry)
            else:
                conflicts.append(entry)

    # ---- missing prerequisites ----
    missing_prereqs = []
    for rule in rules.get("requires", []):
        offenders = [i for i in items if _title_matches(i["title"], rule["if_match"])]
        if not offenders:
            continue
        # the requirement must be satisfied by an asset OTHER than the modules themselves
        satisfies = [i for i in items
                     if _title_matches(i["title"], rule["requires"])
                     and not _title_matches(i["title"], rule["if_match"])]
        if not satisfies:
            missing_prereqs.append({
                "asset": [i["title"] for i in offenders],
                "requires": rule["requires"],
                "reason": rule["reason"],
            })

    return {
        "stack_size": len(items),
        "roles_detected": {rules["roles"][k]["label"]: [i["title"] for i in v]
                           for k, v in role_map.items()},
        "conflicts": conflicts,
        "same_family_notes": family_notes,
        "missing_prerequisites": missing_prereqs,
        "unknown_ids": missing_ids,
        "verdict": "ok" if not conflicts and not missing_prereqs else "issues-found",
    }


def resolve_stack_patterns(patterns: List[str], db_path: str = DB_PATH,
                           all_assets: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Resolve title-pattern list to concrete owned assets (best match each).
    Loads database assets once if all_assets is not pre-populated."""
    if all_assets is None:
        conn = get_connection(db_path)
        cur = conn.execute("SELECT id, title, category, local_path FROM assets")
        all_assets = [dict(r) for r in cur]
        conn.close()

    resolved = []
    for pat in patterns:
        best = None
        for row in all_assets:
            if _title_matches(row["title"], pat):
                if best is None or len(row["title"]) < len(best["title"]):
                    best = dict(row)
        if best:
            entry = dict(best)
            entry["matched_pattern"] = pat
            entry["local"] = bool(entry.pop("local_path", ""))
            resolved.append(entry)
    return resolved


def list_recipes(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    rules = load_rules()
    conn = get_connection(db_path)
    cur = conn.execute("SELECT id, title, category, local_path FROM assets")
    all_assets = [dict(r) for r in cur]
    conn.close()

    out = []
    for recipe in rules.get("recipes", []):
        out.append({
            "name": recipe["name"],
            "engine": recipe.get("engine"),
            "pipeline": recipe.get("pipeline"),
            "notes": recipe.get("notes", ""),
            "owned_matches": resolve_stack_patterns(recipe["stack_patterns"], db_path, all_assets=all_assets),
        })
    return out


if __name__ == "__main__":
    import sys
    print(json.dumps(list_recipes(), indent=2)[:2000])
