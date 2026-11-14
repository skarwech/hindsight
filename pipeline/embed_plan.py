"""Step 4 (F5): decide, before any model call, how each distinct chunk gets its vector.

    .venv/bin/python pipeline/embed_plan.py [--copy-ratio 0.97]     # default: no copies (the shortcut is off)

For every text_hash in data/chunks/chunk_text.parquet (the spelling that gets embedded), in order of first appearance:
  model      embed it with bge-m3;
  copy(a)    reuse the vector of anchor a: the chunk first appeared as a new state of a claim whose previous text
             (its parent) leads to a, and the character similarity (rapidfuzz ratio) to a's text is >= --copy-ratio.
An anchor is always a model row (a copy's anchor is its parent's anchor), and the ratio is taken against the anchor's
text, so chains of small edits cannot drift away from the vector they share.
Parents come from the character-only lineage in data/lineage/ (Step 3), which stays frozen: the semantic re-run writes
elsewhere. Writes data/embed/plan.parquet and data/embed/plan.json (inputs' digests, counts). The embedding job and the
F5 checks follow this file, so a resumed job cannot change a decision.
"""
import argparse, hashlib, json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
CH, LIN, OUT = ROOT / "data/chunks", ROOT / "data/lineage", ROOT / "data/embed"
COPY_RATIO = 1.01   # off. The plan's 0.97 shortcut failed its oracle (F5.5): only 87.4% of 10,180 copies had cosine >= 0.98
                    # with a fresh embedding (93.9% even at ratio >= 0.99); the worst are name-swap vandalism that keeps 97% of
                    # the characters. Embedding every chunk costs ~5 min more at this N and keeps vandal text distinguishable.
PLAN = pa.schema([("text_hash", pa.uint64()), ("action", pa.string()), ("anchor", pa.uint64()), ("parent", pa.uint64()),
                  ("ratio", pa.float32()), ("first_page", pa.uint32()), ("first_ord", pa.uint32()), ("chars", pa.uint32())])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_appearances(d: duckdb.DuckDBPyConnection) -> list[tuple[int, int, int, int | None]]:
    """(text_hash, page_id, ord, parent text_hash) at each hash's first appearance (smallest page_id, ord, pos). The parent
    is the previous text of the claim that holds the chunk there (None for a brand-new claim)."""
    return d.execute(f"""
        WITH firsts AS (
            SELECT text_hash, arg_min(struct_pack(page_id, ord, pos), struct_pack(page_id, ord, pos)) f
            FROM '{CH}/rev_chunks.parquet' GROUP BY text_hash),
        holder AS (
            SELECT f.text_hash, f.f.page_id page_id, f.f.ord ord, i.claim_id
            FROM firsts f JOIN '{LIN}/claim_ids.parquet' i ON i.page_id = f.f.page_id AND i.ord = f.f.ord AND i.pos = f.f.pos),
        prev AS (                                      -- the claim's last state that started before this revision
            SELECT h.text_hash, arg_max(s.text_hash, s.start_ord) parent
            FROM holder h JOIN '{LIN}/states.parquet' s ON s.page_id = h.page_id AND s.claim_id = h.claim_id AND s.start_ord < h.ord
            GROUP BY h.text_hash)
        SELECT h.text_hash, h.page_id, h.ord, p.parent FROM holder h LEFT JOIN prev p USING (text_hash)
        ORDER BY h.page_id, h.ord, h.text_hash""").fetchall()


def decide(copy_ratio: float) -> dict[str, list]:
    """The plan's columns at this copy ratio (nothing written). F5.5 calls it with the plan's 0.97 to measure the shortcut."""
    d = duckdb.connect()
    text = dict(d.execute(f"SELECT text_hash, text FROM '{CH}/chunk_text.parquet'").fetchall())
    rows = first_appearances(d)
    if len(rows) != len(text) or {r[0] for r in rows} != set(text):
        raise RuntimeError("every chunk_text hash needs exactly one first appearance with a claim id")
    action: dict[int, str] = {}; anchor: dict[int, int | None] = {}
    cols: dict[str, list] = {f.name: [] for f in PLAN}
    for h, page, ord_, parent in rows:
        a, r = None, None
        if parent is not None and parent != h:
            if parent not in action:                   # a parent always appears in an earlier revision of the page
                raise RuntimeError(f"parent {parent} of {h} not planned before it")
            a = parent if action[parent] == "model" else anchor[parent]
            r = fuzz.ratio(text[h], text[a]) / 100
        copy = a is not None and r >= copy_ratio
        action[h], anchor[h] = ("copy", a) if copy else ("model", None)
        for k, v in zip(cols, (h, action[h], anchor[h], parent if parent != h else None, r, page, ord_, len(text[h])), strict=True):
            cols[k].append(v)
    return cols


def build(copy_ratio: float) -> dict:
    cols = decide(copy_ratio)
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "plan.parquet.tmp"
    pq.write_table(pa.table(cols, schema=PLAN), tmp); tmp.replace(OUT / "plan.parquet")
    n_copy = sum(1 for x in cols["action"] if x == "copy")
    meta = dict(copy_ratio=copy_ratio, rows=len(cols["text_hash"]), model=len(cols["text_hash"]) - n_copy, copy=n_copy,
                with_parent=sum(1 for p in cols["parent"] if p is not None),
                inputs={p.name: digest(p) for p in (CH / "chunk_text.parquet", CH / "rev_chunks.parquet",
                                                    LIN / "claim_ids.parquet", LIN / "states.parquet")},
                plan_sha256=digest(OUT / "plan.parquet"))
    (OUT / "plan.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy-ratio", type=float, default=COPY_RATIO)
    print(json.dumps({k: v for k, v in build(ap.parse_args().copy_ratio).items() if k != "inputs"}))


if __name__ == "__main__":
    main()
