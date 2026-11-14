"""Step 3 (F4): run the resolver over every revision of the hero pages.

    .venv/bin/python pipeline/lineage.py [--t-edit 0.60]                  # character pass -> data/lineage/
    .venv/bin/python pipeline/lineage.py --semantic [--t-sem 0.88]        # with bge-m3 vectors -> data/lineage_sem/

Reads data/chunks/ and writes data/lineage/ (built in data/lineage.tmp, moved into place when complete):
  claim_ids.parquet (page_id, rev_id, ord, pos, claim_id)                 — aligned with rev_chunks
  events.parquet    (page_id, rev_id, ord, kind, claim_id, sim, rule)      — add | delete | edit | revive | semantic
  states.parquet    (page_id, claim_id, start_ord, end_ord, start_rev, text_hash, exact_hash, n_refs, section)
                    — one row per claim state: the history rows Step 5 loads (end_ord NULL = still current). A state
                    changes when the exact text, the citation count or the section changes.
Hidden revisions (text suppressed, 53) are skipped: their claims carry over unchanged. --semantic adds the resolver's
semantic pass with the vectors of data/embed/store (Step 4) and writes data/lineage_sem/ and logs/lineage_sem.json, so the
character run (which the embedding plan's parents come from) stays as it is. The log records t_edit, t_sem, the
resolver's sha256 and the vector store's content digest, which F4 checks. Step 4 measured the semantic pass and did not
adopt it (F4S: precision of its links 65.5% worst case, far below 95%): Step 5 loads the character run.
"""
import argparse, hashlib, json, shutil, time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from itertools import count
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
from resolver import T_EDIT, T_SEM, Claim, Memory, Para, resolve  # noqa: E402
import numpy as np  # noqa: E402

CH = ROOT / "data/chunks"
_VECTORS: dict[int, np.ndarray] | None = None          # loaded once per worker process for the semantic pass


def vectors() -> dict[int, np.ndarray]:
    global _VECTORS
    if _VECTORS is None:
        from embed_chunks import unit_vectors
        _VECTORS = unit_vectors()
    return _VECTORS


def paths(semantic: bool) -> tuple[Path, Path, Path]:
    """(output dir, its temporary build dir, log file) for the character or the semantic run."""
    name = "lineage_sem" if semantic else "lineage"
    return ROOT / "data" / name, ROOT / "data" / f"{name}.tmp", ROOT / "logs" / f"{name}.json"
IDS = pa.schema([("page_id", pa.uint32()), ("rev_id", pa.uint32()), ("ord", pa.uint32()), ("pos", pa.uint16()), ("claim_id", pa.uint32())])
EVENTS = pa.schema([("page_id", pa.uint32()), ("rev_id", pa.uint32()), ("ord", pa.uint32()), ("kind", pa.string()),
                    ("claim_id", pa.uint32()), ("sim", pa.float32()), ("rule", pa.string())])
STATES = pa.schema([("page_id", pa.uint32()), ("claim_id", pa.uint32()), ("start_ord", pa.uint32()), ("end_ord", pa.uint32()),
                    ("start_rev", pa.uint32()), ("text_hash", pa.uint64()), ("exact_hash", pa.uint64()), ("n_refs", pa.uint32()),
                    ("section", pa.string())])


def page_revisions(page_id: int) -> list[tuple[int, int, bool, list[tuple]]]:
    """[(ord, rev_id, hidden, [(text_hash, exact_hash, n_refs, section, text), ...])] in page order."""
    d = duckdb.connect()
    revs = d.execute(f"SELECT ord, rev_id, hidden FROM '{CH}/revisions.parquet' WHERE page_id = ? ORDER BY ord", [page_id]).fetchall()
    rows = d.execute(f"""SELECT r.rev_id, r.text_hash, r.exact_hash, r.n_refs, r.section, v.text
                         FROM '{CH}/rev_chunks.parquet' r JOIN '{CH}/chunk_variant.parquet' v USING (exact_hash)
                         WHERE r.page_id = ? ORDER BY r.ord, r.pos""", [page_id]).fetchall()
    by_rev = defaultdict(list)
    for rev, *rest in rows:
        by_rev[rev].append(tuple(rest))
    return [(o, r, h, by_rev.get(r, [])) for o, r, h in revs]


def lineage_page(page_id: int, parts: str, t_edit: float = T_EDIT, semantic: bool = False,
                 t_sem: float = T_SEM) -> dict[str, int | float]:
    t0 = time.time()
    ids_cols: dict[str, list] = {f.name: [] for f in IDS}
    ev_cols: dict[str, list] = {f.name: [] for f in EVENTS}
    st_cols: dict[str, list] = {f.name: [] for f in STATES}
    next_id = count(1).__next__
    prev: list[Claim] = []; recent: list[Claim] = []; mem = Memory()
    open_states: dict[int, tuple] = {}                     # claim_id -> (start_ord, start_rev, text_hash, exact_hash, n_refs, section)

    def close(cid: int, end_ord: int | None) -> None:
        s = open_states.pop(cid)
        for k, v in zip(st_cols, (page_id, cid, s[0], end_ord, s[1], s[2], s[3], s[4], s[5])):
            st_cols[k].append(v)

    kinds: Counter = Counter()
    vec = vectors() if semantic else None
    for ord_, rev, hidden, chunks in page_revisions(page_id):
        if hidden:
            continue                                       # text unknown: claims carry over
        cur = [Para(th, text, sec) for th, _, _, sec, text in chunks]
        ids, events = resolve(prev, cur, mem, next_id, t_edit=t_edit, vectors=vec, recent=recent, t_sem=t_sem)
        for pos, cid in enumerate(ids):  # resolve returns exactly one id per paragraph (it raises otherwise)
            for k, v in zip(ids_cols, (page_id, rev, ord_, pos, cid)):
                ids_cols[k].append(v)
        for e in events:
            kinds[e.kind] += 1
            for k, v in zip(ev_cols, (page_id, rev, ord_, e.kind, e.claim_id, e.sim, e.rule)):
                ev_cols[k].append(v)
        now = {cid: (th, eh, nr, sec) for cid, (th, eh, nr, sec, _) in zip(ids, chunks, strict=True)}
        for cid in [c for c in open_states if c not in now or open_states[c][2:] != now[c]]:
            close(cid, ord_)                               # gone, or its text / citations / section changed
        for cid, s in now.items():
            if cid not in open_states:
                open_states[cid] = (ord_, rev) + s
        deleted = {e.claim_id for e in events if e.kind == "delete"}
        recent = [c for c in prev if c.claim_id in deleted]
        prev = [Claim(cid, p.hash, p.text, p.section) for cid, p in zip(ids, cur)]
    for cid in list(open_states):
        close(cid, None)
    pq.write_table(pa.table(ids_cols, schema=IDS), f"{parts}/ids_{page_id}.parquet")
    pq.write_table(pa.table(ev_cols, schema=EVENTS), f"{parts}/events_{page_id}.parquet")
    pq.write_table(pa.table(st_cols, schema=STATES), f"{parts}/states_{page_id}.parquet")
    return dict(page_id=page_id, claims=next_id() - 1, current=len(prev), states=len(st_cols["claim_id"]),
                seconds=round(time.time() - t0, 2), **kinds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t-edit", type=float, default=T_EDIT)
    ap.add_argument("--semantic", action="store_true")
    ap.add_argument("--t-sem", type=float, default=T_SEM)
    args = ap.parse_args()
    OUT, TMP, LOG = paths(args.semantic)
    if args.semantic:                                      # a partial store would silently skip pairs: refuse it
        have = set(vectors())
        need = {h for (h,) in duckdb.connect().execute(f"SELECT DISTINCT text_hash FROM '{CH}/rev_chunks.parquet'").fetchall()}
        if need - have:
            raise SystemExit(f"vector store lacks {len(need - have):,} chunk hashes: finish pipeline/embed_chunks.py first")
    pages = [p for (p,) in duckdb.connect().execute(f"SELECT page_id FROM '{CH}/revisions.parquet' GROUP BY 1 ORDER BY COUNT(*) DESC").fetchall()]
    shutil.rmtree(TMP, ignore_errors=True); parts = TMP / "parts"; parts.mkdir(parents=True)
    t0 = time.time()
    with ProcessPoolExecutor(10) as pool:                  # a failing page re-raises here
        n = len(pages)
        stats = list(pool.map(lineage_page, pages, [str(parts)] * n, [args.t_edit] * n, [args.semantic] * n, [args.t_sem] * n))
    d = duckdb.connect()
    for name, order in (("claim_ids", "page_id, ord, pos"), ("events", "page_id, ord, kind, claim_id"), ("states", "page_id, claim_id, start_ord")):
        prefix = {"claim_ids": "ids", "events": "events", "states": "states"}[name]
        d.execute(f"COPY (SELECT * FROM read_parquet('{parts}/{prefix}_*.parquet') ORDER BY {order}) TO '{TMP}/{name}.parquet' (FORMAT parquet)")
    shutil.rmtree(parts)
    n_ids = d.execute(f"SELECT COUNT(*) FROM '{TMP}/claim_ids.parquet'").fetchall()[0][0]
    n_chunks = d.execute(f"SELECT COUNT(*) FROM '{CH}/rev_chunks.parquet'").fetchall()[0][0]
    assert len(stats) == len(pages) and n_ids == n_chunks, "a claim id for every (revision, chunk) row"
    tot = Counter()
    for s in stats:
        tot.update({k: v for k, v in s.items() if k in ("claims", "current", "states", "add", "delete", "edit", "revive", "semantic")})
    resolver_sha = hashlib.sha256((ROOT / "pipeline/resolver.py").read_bytes()).hexdigest()
    extra = {}
    if args.semantic:
        from embed_chunks import store_digest
        extra = dict(t_sem=args.t_sem, vectors_sha256=store_digest())
    summary = dict(pages=len(pages), t_edit=args.t_edit, semantic_pass=args.semantic, **extra, resolver_sha256=resolver_sha, seconds=round(time.time() - t0, 1), **tot, per_page=stats)
    shutil.rmtree(OUT, ignore_errors=True); TMP.rename(OUT)
    LOG.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_page"}))


if __name__ == "__main__":
    main()
