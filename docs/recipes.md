# `data/recipes.json` — stack rules and recipes

This file is Quartermaster's stack knowledge base. It is read at runtime by
[`src/stack_rules.py`](../src/stack_rules.py); nothing is compiled or cached, so
editing the file and re-running a query is the whole edit loop.

It drives three things:

| Surface | What it uses |
| :--- | :--- |
| `validate_stack(asset_ids)` (MCP) | `roles`, `requires` |
| `list_stack_recipes()` (MCP) | `recipes` |
| `GET /api/recipes` (web UI) | `recipes` |

If the file is missing or unparseable, Quartermaster logs a warning and falls
back to `{"roles": {}, "recipes": []}` — the linter goes quiet rather than
crashing.

---

## How matching works

Every pattern in this file — `match`, `if_match`, `requires`, `stack_patterns` —
is matched against **asset titles** with the same function, and it is **not** a
plain substring test.

```python
rx = re.compile(r"\b" + re.escape(alt) + r"(?![a-z])")
```

Two consequences worth internalising:

- **Word-boundary aware.** `enviro` matches *"Enviro 3 — Sky and Weather"* but
  **not** *"Environment Pack"*. This is deliberate; without it, short patterns
  swallow half the library.
- **`|` means alternation.** `"better lit|better shaders"` matches either. Spaces
  inside an alternative are literal, so `"dynamic water"` matches that phrase.

Matching is case-insensitive. There is no regex support beyond `|` — your
patterns are escaped, so `.` and `*` are literal characters.

---

## `roles` — what an asset *does*

A role is a job in a project that usually only one asset should hold.

```json
"water_system": {
  "label": "Water system",
  "exclusive": true,
  "match": ["kws", "crest", "dynamic water", "river editor"]
}
```

| Field | Meaning |
| :--- | :--- |
| `label` | Human-readable name, shown in linter output |
| `exclusive` | `true` → two assets filling this role is a **conflict**. `false` → the role is still detected and reported, but never flagged |
| `match` | List of title patterns; any one matching assigns the role |

Set `exclusive: false` for roles where stacking is normal. `terrain_shader` ships
that way, because a MicroSplat core plus its modules legitimately coexist.

### The same-family exception

Two assets filling one exclusive role are *not* reported as a conflict if they
matched the **exact same pattern string** in your rule. Quartermaster tracks which
pattern in `"match"` triggered for each asset.

So in:
```json
"terrain_generator": {
  "exclusive": true,
  "match": ["microverse", "gaia", "digger", "infinitelands"]
}
```

*"MicroVerse — Core"* and *"MicroVerse — Roads"* both matched `"microverse"`, so they
are recognised as the same product line and downgraded to a softer note under `same_family_notes`:

> These look like modules/versions of the same product family. Verify which
> variant you actually need before importing several.

While *"MicroVerse — Core"* (matched `"microverse"`) and *"Gaia Pro"* (matched `"gaia"`)
matched different patterns in an exclusive role, and are correctly flagged as a **conflict**.

If a vendor splits brand names across variants (e.g. `Better Lit Shader` and `Better Shaders`),
combine them with `|` in a single pattern entry (`"better lit|better shaders"`), and they will
group into the same family automatically.

---

## `requires` — prerequisites

```json
{
  "if_match": "microsplat trax",
  "requires": "microsplat",
  "reason": "Trax is a MicroSplat module and needs the core terrain shader."
}
```

If any asset in the stack matches `if_match`, then some **other** asset must
match `requires`, or a `missing_prerequisites` entry is emitted carrying your
`reason` verbatim.

The "other" matters. The satisfying asset is explicitly excluded from also
matching `if_match`, so *"MicroSplat Trax"* cannot satisfy its own requirement
just because its title contains `microsplat`. Keep `requires` broad enough to hit
the core product but narrow enough not to hit the module — `|` helps:

```json
{ "if_match": "stampit", "requires": "microverse|microsplat", "reason": "..." }
```

---

## `recipes` — curated stacks

```json
{
  "name": "Photorealistic outdoor biome (Unity HDRP)",
  "engine": "unity",
  "pipeline": "HDRP",
  "stack_patterns": ["microverse", "microsplat", "better shaders|better lit"],
  "notes": "Non-destructive terrain authoring -> procedural texturing -> ..."
}
```

`name` and `stack_patterns` are required; `engine`, `pipeline` and `notes` are
optional passthrough metadata.

Each pattern resolves to **at most one** owned asset. When several titles match,
the **shortest title wins** — a deliberate bias toward the core product over its
modules, so `microsplat` resolves to *"MicroSplat"* rather than *"MicroSplat —
Tessellation and Parallax"*. Patterns matching nothing you own are silently
dropped, so a recipe naturally degrades to the parts of it you actually have.

The result carries `matched_pattern` and a `local` boolean per asset, so an agent
can tell what's on disk versus cloud-only.

---

## Editing checklist

1. Edit `data/recipes.json`.
2. Validate the JSON — a syntax error silently disables the whole linter.
3. Re-run: `python -c "import json,src.stack_rules as s; print(json.dumps(s.list_recipes(), indent=2)[:800])"`
4. For roles, test against a real stack: `validate_stack(["unity_123", "unity_456"])`.

If a pattern isn't matching, the cause is almost always the word boundary — check
whether your pattern is a prefix of a longer word in the title rather than a word
on its own.
