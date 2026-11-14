"""Step 2 (F3): chunk every revision of the hero pages.

    .venv/bin/python pipeline/chunk_hero.py

Writes data/chunks/ (built in data/chunks.tmp and moved into place only when every post-condition holds):
  rev_chunks.parquet    (page_id, rev_id, ord, pos, section, text_hash, exact_hash, n_refs) — the plan's (page, rev) -> [text_hash...]
  chunk_text.parquet    (text_hash, text, chars, bytes, spellings, rows)  — one row per normalised text: N sizes Step 4;
                        text is the most frequent exact spelling, the one to embed
  chunk_variant.parquet (exact_hash, text_hash, text)                    — every exact spelling, for verbatim display
  revisions.parquet     (page_id, rev_id, ord, n_chunks, chunk_chars, hidden)
Pages run in parallel; revisions within a page in page_state order (row_start, rev_id), cached by sha1 (reverts repeat texts).
"""
import csv, json, shutil, sys, time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import xxhash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from common import connect  # noqa: E402
from chunker import Chunk, chunk_revision  # noqa: E402

OUT, TMP = ROOT / "data/chunks", ROOT / "data/chunks.tmp"
M_GLOB = str(ROOT / "data/m/versions-*.parquet")
CHUNKS = pa.schema([("page_id", pa.uint32()), ("rev_id", pa.uint32()), ("ord", pa.uint32()), ("pos", pa.uint16()),
                    ("section", pa.string()), ("text_hash", pa.uint64()), ("exact_hash", pa.uint64()), ("n_refs", pa.uint32())])
VARIANTS = pa.schema([("exact_hash", pa.uint64()), ("text_hash", pa.uint64()), ("text", pa.string()), ("rows", pa.uint64())])
REVS = pa.schema([("page_id", pa.uint32()), ("rev_id", pa.uint32()), ("ord", pa.uint32()), ("n_chunks", pa.uint32()),
                  ("chunk_chars", pa.uint64()), ("hidden", pa.bool_())])


def exact_hash(text: str) -> int:
    return xxhash.xxh64_intdigest(text.encode("utf-8"))


def chunk_page(page_id: int, path: str, revs: list[int], parts: str) -> dict:
    t0 = time.time()
    rows = duckdb.connect().execute("SELECT rev_id, sha1, text FROM read_parquet(?) WHERE page_id = ?", [path, page_id]).fetchall()
    src = {r[0]: (r[1], r[2]) for r in rows}
    if set(src) != set(revs):
        raise ValueError(f"page {page_id}: Parquet and page_state disagree on its revisions")
    cache: dict[str, list[Chunk]] = {}
    stats: Counter = Counter()
    chunks_cols: dict[str, list] = {f.name: [] for f in CHUNKS}
    revs_cols: dict[str, list] = {f.name: [] for f in REVS}
    variants: Counter = Counter()                            # (exact_hash, text_hash, text) -> rows using it
    for ord_, rev in enumerate(revs):
        sha1, text = src[rev]
        chunks = cache.get(sha1) if sha1 else None
        if chunks is None:
            chunks = chunk_revision(text, stats)
            if sha1:
                cache[sha1] = chunks
        for pos, c in enumerate(chunks):
            e = exact_hash(c.text)
            for k, v in zip(chunks_cols, (page_id, rev, ord_, pos, c.section, c.text_hash, e, c.n_refs)):
                chunks_cols[k].append(v)
            variants[(e, c.text_hash, c.text)] += 1
        for k, v in zip(revs_cols, (page_id, rev, ord_, len(chunks), sum(len(c.text) for c in chunks), sha1 is None)):
            revs_cols[k].append(v)
    pq.write_table(pa.table(chunks_cols, schema=CHUNKS), f"{parts}/chunks_{page_id}.parquet")
    pq.write_table(pa.table({"exact_hash": [k[0] for k in variants], "text_hash": [k[1] for k in variants],
                             "text": [k[2] for k in variants], "rows": list(variants.values())}, schema=VARIANTS),
                   f"{parts}/variants_{page_id}.parquet")
    pq.write_table(pa.table(revs_cols, schema=REVS), f"{parts}/revs_{page_id}.parquet")
    return dict(page_id=page_id, revisions=len(revs), distinct_texts=len(cache), seconds=round(time.time() - t0, 1), **stats)


def main() -> None:
    with open(ROOT / "docs/hero.tsv", encoding="utf-8") as f:
        hero = [int(r["page_id"]) for r in csv.DictReader(f, delimiter="\t")]
    ids = ",".join(map(str, hero))
    cur = connect("wikihistory").cursor()
    cur.execute(f"SELECT page_id, rev_id FROM page_state FOR SYSTEM_TIME ALL WHERE page_id IN ({ids}) ORDER BY page_id, row_start, rev_id")
    order: dict[int, list[int]] = {p: [] for p in hero}
    for p, r in cur.fetchall():
        order[p].append(r)
    files = dict(duckdb.connect().execute(f"SELECT DISTINCT page_id, filename FROM read_parquet(?, filename=true) WHERE page_id IN ({ids})",
                                          [M_GLOB]).fetchall())
    if set(files) != set(hero) or any(not order[p] for p in hero):
        sys.exit("hero pages missing from dataset M or page_state")
    shutil.rmtree(TMP, ignore_errors=True); parts = TMP / "parts"; parts.mkdir(parents=True)
    t0 = time.time()
    pages = sorted(hero, key=lambda p: -len(order[p]))           # biggest first, for balance
    with ProcessPoolExecutor(10) as pool:                         # a failing page re-raises here
        stats = list(pool.map(chunk_page, pages, [files[p] for p in pages], [order[p] for p in pages], [str(parts)] * len(pages)))
    d = duckdb.connect()
    d.execute(f"COPY (SELECT * FROM read_parquet('{parts}/chunks_*.parquet') ORDER BY page_id, ord, pos) TO '{TMP}/rev_chunks.parquet' (FORMAT parquet)")
    d.execute(f"COPY (SELECT * FROM read_parquet('{parts}/revs_*.parquet') ORDER BY page_id, ord) TO '{TMP}/revisions.parquet' (FORMAT parquet)")
    d.execute(f"""CREATE TABLE v AS SELECT exact_hash, any_value(text_hash) AS text_hash, any_value(text) AS text, SUM(rows) AS rows
                  FROM read_parquet('{parts}/variants_*.parquet') GROUP BY exact_hash""")
    d.execute(f"COPY (SELECT exact_hash, text_hash, text FROM v ORDER BY exact_hash) TO '{TMP}/chunk_variant.parquet' (FORMAT parquet)")
    # the representative text of a hash: its most frequent exact spelling (ties: the smallest exact_hash)
    d.execute(f"""COPY (SELECT text_hash, arg_max(text, rows * 1e20 - exact_hash::DOUBLE) AS text,
                               length(arg_max(text, rows * 1e20 - exact_hash::DOUBLE)) AS chars,
                               octet_length(encode(arg_max(text, rows * 1e20 - exact_hash::DOUBLE))) AS bytes,
                               COUNT(*) AS spellings, SUM(rows) AS rows
                        FROM v GROUP BY text_hash ORDER BY text_hash) TO '{TMP}/chunk_text.parquet' (FORMAT parquet)""")
    shutil.rmtree(parts)
    n_rows, n_revs, hidden = d.execute(f"SELECT (SELECT COUNT(*) FROM '{TMP}/rev_chunks.parquet'), COUNT(*), SUM(hidden::INT) FROM '{TMP}/revisions.parquet'").fetchall()[0]
    n_unique, merged, text_rows = d.execute(f"SELECT COUNT(*), COUNT(*) FILTER (WHERE spellings > 1), SUM(rows) FROM '{TMP}/chunk_text.parquet'").fetchall()[0]
    # post-conditions before the old output is replaced
    assert len(stats) == len(hero) and n_revs == sum(len(order[p]) for p in hero), "revisions missing"
    assert text_rows == n_rows, "chunk_text does not account for every (revision, chunk) row"
    tot = Counter()
    for s in stats:
        tot.update({k: v for k, v in s.items() if k in ("placeholder_fallback", "orphan_refs")})
    summary = dict(pages=len(hero), revisions=n_revs, hidden_revisions=int(hidden), chunk_rows=n_rows, N=n_unique,
                   hashes_with_several_spellings=merged, placeholder_fallbacks=tot["placeholder_fallback"],
                   orphan_refs=tot["orphan_refs"], seconds=round(time.time() - t0, 1), per_page=stats)
    shutil.rmtree(OUT, ignore_errors=True); TMP.rename(OUT)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs/chunk_hero.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_page"}))


if __name__ == "__main__":
    main()
