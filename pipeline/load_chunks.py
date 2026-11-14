"""Step 4 (F5): load the `chunk` table (one row per distinct text, its vector, HNSW and FULLTEXT) from the vector store.

    .venv/bin/python pipeline/load_chunks.py          # needs a complete data/embed/store (pipeline/embed_chunks.py)

The DDL is db/schema.sql's `CREATE TABLE chunk` (M=16, cosine). Rows go in a fixed order (text_hash ascending, since HNSW
recall depends on insertion order) in committed batches; a re-run skips the hashes already in the table, so the load
can be killed and resumed. Before skipping, it checks that the table's vector index is the schema's (M=16, cosine) and
that every row already there equals the store (text and vector bytes), and refuses otherwise: a table loaded from an
older store must be dropped first. Every run is appended to logs/load_chunks.jsonl with the store's content digest.
"""
import json, sys, time
from pathlib import Path

import duckdb
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from common import connect, split_sql  # noqa: E402  (shared DB helpers live in eval/common.py)
from embed_chunks import read_store, store_digest  # noqa: E402

CH, EMB, LOG = ROOT / "data/chunks", ROOT / "data/embed", ROOT / "logs/load_chunks.jsonl"
BATCH = 500


def ddl() -> str:
    return next(s for s in split_sql((ROOT / "db/schema.sql").read_text(encoding="utf-8")) if s.startswith("CREATE TABLE chunk"))


def rows() -> list[tuple[int, str, bytes]]:
    """(text_hash, text, float32 little-endian vector) in text_hash order; refuses an incomplete or extra store."""
    hashes, _, vec = read_store()
    text = dict(duckdb.connect().execute(f"SELECT text_hash, text FROM '{CH}/chunk_text.parquet'").fetchall())
    if len(hashes) != len(text) or set(hashes.tolist()) != set(text):
        raise RuntimeError(f"store has {len(hashes):,} vectors for {len(text):,} chunks: finish pipeline/embed_chunks.py first")
    order = np.argsort(hashes, kind="stable")
    v = vec.astype("<f4", copy=False)
    return [(int(hashes[k]), text[int(hashes[k])], v[k].tobytes()) for k in order]


def main() -> None:
    t0 = time.time()
    data = rows()
    con = connect("wikihistory"); cur = con.cursor()
    cur.execute(ddl().replace("CREATE TABLE chunk", "CREATE TABLE IF NOT EXISTS chunk", 1))
    cur.execute("SHOW CREATE TABLE chunk"); ddl_now = cur.fetchone()[1]
    if "VECTOR KEY `emb` (`emb`) `M`='16' `DISTANCE`='cosine'" not in ddl_now or "FULLTEXT KEY `text`" not in ddl_now:
        raise RuntimeError(f"existing chunk table does not have the schema's indexes:\n{ddl_now}")
    want = {h: (t, e) for h, t, e in data}
    cur.execute("SELECT text_hash, text, emb FROM chunk")
    done, stale = set(), 0
    for h, t, e in cur.fetchall():
        done.add(h); stale += want.get(h) != (t, bytes(e))
    if stale:
        raise RuntimeError(f"{stale} rows in chunk differ from the store or are not in it: drop the table and load again")
    todo = [r for r in data if r[0] not in done]
    con.autocommit = False
    for i in range(0, len(todo), BATCH):
        cur.executemany("INSERT INTO chunk (text_hash, text, emb) VALUES (?, ?, ?)", todo[i:i + BATCH]); con.commit()
        if (i // BATCH) % 10 == 0:
            print(f"{len(done) + i + len(todo[i:i + BATCH]):,} rows; {(i + BATCH) / (time.time() - t0):.0f}/s", flush=True)
    con.autocommit = True
    cur.execute("SELECT COUNT(*) FROM chunk"); n = cur.fetchone()[0]
    rec = dict(started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)), store_sha256=store_digest(), seconds=round(time.time() - t0, 1),
               rows_before=len(done), inserted=len(todo), rows_after=n, complete=n == len(data))
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))


if __name__ == "__main__":
    main()
