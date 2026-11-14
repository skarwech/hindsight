"""Step 4 (F5): embed the chunks the plan marks `model` with bge-m3 (Ollama), then fill the `copy` rows from their
anchors. Safe to kill at any moment and re-run: it resumes where it stopped.

    .venv/bin/python pipeline/embed_chunks.py        # needs data/embed/plan.parquet (pipeline/embed_plan.py)

The store is data/embed/store/part-NNNNN.parquet (text_hash, source, emb float32[1024]); each shard is written to a
temporary file and renamed, so a kill leaves whole shards only. On start the job reads the shards, refuses a store with
duplicate or unplanned hashes or rows whose source (model/copy) disagrees with the plan (move stale shards away after a
re-plan), and embeds only what is missing, in plan order. Shards are numbered after the highest existing one. Every run
is appended to logs/embed.jsonl with the plan's sha256.
"""
import json, os, sys, time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import requests

ROOT = Path(__file__).resolve().parents[1]
EMB, CH = ROOT / "data/embed", ROOT / "data/chunks"
STORE, LOG = EMB / "store", ROOT / "logs/embed.jsonl"
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OPTS = {"num_ctx": 8192, "num_batch": 8192}            # both, or Ollama cuts long inputs [S3]
DIM, CALL, SHARD = 1024, 64, 512                       # vector size; texts per model call; rows per shard
SCHEMA = pa.schema([("text_hash", pa.uint64()), ("source", pa.string()), ("emb", pa.list_(pa.float32(), DIM))])


def embed(texts: list[str]) -> np.ndarray:
    r = requests.post(f"{OLLAMA}/api/embed", timeout=600, json={"model": "bge-m3", "input": texts, "truncate": False, "options": OPTS})
    r.raise_for_status()
    v = np.asarray(r.json()["embeddings"], dtype=np.float32)
    if v.shape != (len(texts), DIM) or not np.isfinite(v).all():
        raise RuntimeError(f"bad embedding batch: shape {v.shape}")
    return v


def read_store(store: Path = STORE) -> tuple[np.ndarray, list[str], np.ndarray]:
    """(text_hash array, source list, vectors) over every whole shard, in shard order."""
    parts = sorted(store.glob("part-*.parquet"))
    if not parts:
        return np.zeros(0, np.uint64), [], np.zeros((0, DIM), np.float32)
    t = pa.concat_tables([pq.read_table(p, schema=SCHEMA) for p in parts])
    vec = t.column("emb").combine_chunks().values.to_numpy(zero_copy_only=False).reshape(-1, DIM)
    return t.column("text_hash").to_numpy(), t.column("source").to_pylist(), vec


def store_digest(store: Path = STORE) -> str:
    """sha256 over (text_hash, vector) in text_hash order: the store's content, whatever its shard layout."""
    import hashlib
    hashes, _, vec = read_store(store)
    order = np.argsort(hashes, kind="stable")
    h = hashlib.sha256(hashes[order].astype("<u8").tobytes())
    h.update(np.ascontiguousarray(vec[order], dtype="<f4").tobytes())
    return h.hexdigest()


def unit_vectors(store: Path = STORE) -> dict[int, np.ndarray]:
    """text_hash -> unit vector, for the resolver's semantic pass."""
    hashes, _, vec = read_store(store)
    vec = vec / np.linalg.norm(vec, axis=1, keepdims=True)
    return {int(h): vec[k] for k, h in enumerate(hashes)}


def write_shard(store: Path, hashes: list[int], source: str, vec: np.ndarray) -> Path:
    n = 1 + max((int(p.stem.split("-")[1]) for p in store.glob("part-*.parquet")), default=-1)
    path = store / f"part-{n:05d}.parquet"; tmp = store / f".{path.name}.tmp"
    emb = pa.FixedSizeListArray.from_arrays(pa.array(vec.reshape(-1), pa.float32()), DIM)
    pq.write_table(pa.table([pa.array(hashes, pa.uint64()), pa.array([source] * len(hashes)), emb], schema=SCHEMA), tmp)
    os.replace(tmp, path)
    return path


def run(store: Path = STORE, limit: int | None = None) -> dict:
    """Embed what the store lacks. limit stops after that many model rows (tests)."""
    import duckdb
    store.mkdir(parents=True, exist_ok=True)
    for t in store.glob(".*.tmp"):
        t.unlink()                                     # a shard the last run was killed while writing
    d = duckdb.connect()
    plan = d.execute(f"""SELECT p.text_hash, p.action, p.anchor, c.text FROM '{EMB}/plan.parquet' p
                         JOIN '{CH}/chunk_text.parquet' c USING (text_hash) ORDER BY p.first_page, p.first_ord, p.text_hash""").fetchall()
    have, have_src, vec = read_store(store)
    if len(set(have.tolist())) != len(have) or not set(have.tolist()) <= {h for h, *_ in plan}:
        raise RuntimeError("store holds duplicate or unplanned hashes: refusing to continue")
    action = {h: a for h, a, _, _ in plan}
    stale = sum(action[int(h)] != s for h, s in zip(have, have_src, strict=True))
    if stale:
        raise RuntimeError(f"{stale} store rows disagree with the plan (model vs copy): move the stale shards away and re-run")
    done = set(have.tolist()); t0 = time.time(); before = len(done)
    todo = [(h, t) for h, a, _, t in plan if a == "model" and h not in done][:limit]
    for i in range(0, len(todo), SHARD):
        part = todo[i:i + SHARD]
        v = np.vstack([embed([t for _, t in part[j:j + CALL]]) for j in range(0, len(part), CALL)])
        write_shard(store, [h for h, _ in part], "model", v)
        rate = (i + len(part)) / (time.time() - t0)
        print(f"model {before + i + len(part):,} rows in store; {rate:.1f}/s", flush=True)
    copies = [(h, a) for h, act, a, _ in plan if act == "copy" and h not in done]
    if limit is None and copies:
        have, _, vec = read_store(store)
        row = {int(h): k for k, h in enumerate(have)}
        missing = [a for _, a in copies if a not in row]
        if missing:
            raise RuntimeError(f"{len(missing)} copy anchors have no vector")
        for i in range(0, len(copies), SHARD * 8):
            part = copies[i:i + SHARD * 8]
            write_shard(store, [h for h, _ in part], "copy", vec[[row[a] for _, a in part]])
    have, src, _ = read_store(store)
    plan_meta = EMB / "plan.json"
    plan_sha = json.loads(plan_meta.read_text(encoding="utf-8"))["plan_sha256"] if plan_meta.exists() else None
    rec = dict(started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)), plan_sha256=plan_sha, seconds=round(time.time() - t0, 1),
               rows_before=before, model_embedded=len(todo), copies_written=len(copies) if limit is None else 0,
               rows_after=len(have), model_rows=src.count("model"), copy_rows=src.count("copy"),
               model_rate=round(len(todo) / max(time.time() - t0, 1e-9), 1), complete=len(have) == len(plan))
    if store == STORE:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


if __name__ == "__main__":
    print(json.dumps(run(limit=int(sys.argv[1]) if len(sys.argv) > 1 else None)))
