# `data/concepts.json` — visual concept vocabulary

This file defines the vocabulary Quartermaster uses to describe your assets from
their **screenshots** rather than their titles. It is read by
[`src/vision.py`](../src/vision.py) during `python -m src.vision build`.

This is the most useful file to edit if you work outside game development. The
shipped vocabulary is game-shaped — *dungeon prison*, *sci-fi corridor*,
*first person weapon viewmodel*. Someone doing architectural visualisation,
film previs, or product rendering wants a completely different list, and gets it
by editing one file. No code changes.

```json
{
  "threshold_z": 1.5,
  "concepts": ["gothic architecture", "medieval village", "dense forest", "..."]
}
```

---

## How tagging works

1. Every gallery image is embedded with CLIP (`Qdrant/clip-ViT-B-32`) and stored
   in `image_vectors`. **This step is independent of this file** — image vectors
   power visual search whether or not any concept ever matches.
2. Each concept string is embedded with CLIP-text.
3. Every image is scored against every concept — cosine similarity.
4. Scores are **z-scored per concept column** across the whole corpus, so a
   concept that scores moderately high on everything doesn't win by default.
5. Surviving `(asset, concept)` pairs are ranked by `cosine × z` and written to
   `assets.vision_tags` as a JSON array.

Tags feed three places: category classification in `src/ingest.py`, the FTS5
`vision_tags` column (so they're keyword-searchable), and the **Visual concepts**
row in the desktop detail panel.

---

## The three gates

A pair must clear **all three** to become a tag:

| Gate | Value | Configurable in this file? |
| :--- | :--- | :--- |
| Absolute cosine floor | `>= 0.24` | **No** — `min_cosine` in `_mine_concepts()` |
| Relative outlier | `z >= threshold_z` | **Yes** |
| Per-asset cap | top **3** by `cosine × z` | **No** — `max_tags_per_asset` |

> **Heads-up on the default.** `_mine_concepts()` declares `threshold_z=2.2`, but
> `build()` passes the value loaded from this file, so **the file always wins**.
> Shipped as `1.5`, that's the live gate — the function default is never used.
> Don't read the signature and assume 2.2 is in effect.

Tagging is **skipped entirely** when fewer than 50 image vectors are indexed;
z-scoring needs a corpus to calibrate against. You'll see:

```
[vision] only N vectors indexed — skipping tagging until corpus is larger
```

### Why the cosine floor exists

Z-score alone is not enough, and the failure is subtle. In a normal distribution
roughly 6.7% of samples clear `z >= 1.5`. An asset with 15 gallery screenshots
therefore has a `1 - (1 - 0.067)^15 ≈ 65%` chance that *at least one* image
randomly clears the bar for *any given* concept — because tags aggregate as a max
over images. Without an absolute floor, multi-screenshot assets accumulate
statistical noise.

Concretely, that produced a grass shader tagged *"alien planet landscape"* and a
river material tagged *"ancient temple ruins"*, with 8.9 tags per asset and every
one of 43 concepts firing. The cosine floor and the top-3 cap are what fixed it.

---

## Tuning

There is no correct `threshold_z` — it depends on your vocabulary size, how
distinct your concepts are, and how visually varied your library is. Tune by
**inspecting the distribution**, not by picking a number.

```bash
python - <<'PY'
import sqlite3, json
from collections import Counter
c = sqlite3.connect("data/assets.db"); cnt = Counter(); n = 0; sizes = []
for (v,) in c.execute("SELECT vision_tags FROM assets WHERE vision_tags NOT IN ('','[]')"):
    t = json.loads(v); n += 1; sizes.append(len(t)); cnt.update(t)
print(f"tagged={n}  avg={sum(sizes)/max(n,1):.1f}  max={max(sizes)}")
for tag, k in cnt.most_common(8):
    print(f"  {k:4} ({100*k/n:4.1f}%)  {tag}")
PY
```

What healthy output looks like, against a real ~1,800-asset vault:

| Signal | Too loose | Healthy | Too strict |
| :--- | :--- | :--- | :--- |
| Avg tags/asset | 8.9 | **2–3** | 0 |
| Top concept share | 29% | **under ~15%** | — |
| Concepts never firing | 0 of 43 | a few | most |

If the top concept is on a quarter of your library, it isn't describing anything.
If nothing is tagged at all, the gate is above your corpus ceiling — `2.0` on a
43-concept vocabulary produced exactly zero tags here.

Re-run after every change, and **re-run classification too**, since categories are
partly derived from these tags:

```bash
python -m src.vision build
python -c "import src.db as d; print(d.reclassify_all_assets(), 'rows reclassified')"
```

---

## Writing good concepts

- **Be visually specific.** *"dense forest"* works; *"nature"* is too abstract for
  CLIP to separate from half your library.
- **Two to four words.** Single words are ambiguous; long phrases dilute the
  embedding.
- **Describe the image, not the product.** *"first person weapon viewmodel"*
  beats *"FPS asset pack"* — CLIP sees pixels, not marketing.
- **Keep them mutually distinct.** Near-duplicates split the same score and both
  fail the top-3 cut.
- **Vocabulary size affects the gate.** Adding concepts changes the per-column
  statistics, so re-check the distribution after a large edit.

---

## Commands

```bash
python -m src.vision build [--limit N]   # embed galleries, then mine concepts
python -m src.vision status              # coverage: vectors, tagged assets
python -m src.vision query "<text>"      # visual search, bypasses tags entirely
```

`query` scores your text directly against image vectors, so it works even with an
empty vocabulary — useful for checking whether a weak result is a *tagging*
problem or an *embedding* problem.
