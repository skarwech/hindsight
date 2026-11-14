"""Step 5 (F6): load the deep layer, the claim and layout history rows (and text_variant), on page_state's bumped times.

    .venv/bin/python pipeline/load_claims.py                          # into wikihistory; resumable: loaded pages are skipped
    .venv/bin/python pipeline/load_claims.py --fresh                  # drop claim, layout and text_variant first
    .venv/bin/python pipeline/load_claims.py --schema smoke --fresh   # a second copy (F6 idempotence, fault injection)

Input: the character-only lineage in data/lineage/ (validated in Step 3; Step 4 measured the semantic run and did not adopt
it) and the chunker's output. Each claim state (states.parquet) becomes one claim row: row_start is page_state's row_start
of the revision that opened the state, row_end that of the revision that closed it (none: the current row). Layout gets
one row per change of reading order (the claim ids of each visible revision, in order). Hidden revisions change nothing:
their text is unknown, so claims and layout carry over. Every time is a page_state row_start of the same page, so AS OF
joins across page_state, claim and layout agree. Each batch of whole pages is one transaction; a page counts as loaded
once it has layout rows. The inputs are validated before --fresh drops anything. logs/load_claims_<schema>.json keeps
every run since the last --fresh (counts, timing) and the digests of the files read.
A claim row opens whenever its state changes: text, exact spelling, citation count or section, or a revival after a
deletion. rev_id, editor, anon and comment are those of the revision that opened that state, which for a spelling,
citation or section change is not who wrote the text: Step 6 and 7 attribute text through the lineage events.
"""
import argparse, hashlib, json, sys, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from common import connect, split_sql  # noqa: E402  (shared DB helpers live in eval/common.py)
from revisions import fmt  # noqa: E402

CH, LIN = ROOT / "data/chunks", ROOT / "data/lineage"
TABLES = ("claim", "layout", "text_variant")
BATCH_ROWS = 5000
CLAIM_COLS = "page_id, claim_id, text_hash, exact_hash, section, rev_id, editor, anon, n_refs, comment, row_start"
LAYOUT_COLS = "page_id, rev_id, claim_order, row_start"
INPUTS = (LIN / "states.parquet", LIN / "claim_ids.parquet", CH / "revisions.parquet", CH / "chunk_variant.parquet")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ddl(name: str) -> str:
    return next(s for s in split_sql((ROOT / "db/schema.sql").read_text(encoding="utf-8")) if s.startswith(f"CREATE TABLE {name} ("))


def source() -> tuple[dict, dict, dict, dict, dict]:
    """(revisions per page, states per page, claim order per (page, ord), page_state time and metadata per (page, rev))."""
    log = json.loads((ROOT / "logs/lineage.json").read_text(encoding="utf-8"))
    if log.get("semantic_pass") or "t_sem" in log:
        raise SystemExit("data/lineage is not the character run: Step 5 loads the character-only lineage")
    d = duckdb.connect()
    revs: dict[int, list] = defaultdict(list)
    for p, o, r, hidden in d.execute(f"SELECT page_id, ord, rev_id, hidden FROM '{CH}/revisions.parquet' ORDER BY page_id, ord").fetchall():
        revs[p].append((o, r, hidden))
    states: dict[int, list] = defaultdict(list)
    for p, *rest in d.execute(f"""SELECT page_id, claim_id, start_ord, end_ord, start_rev, text_hash, exact_hash, n_refs, section
                                  FROM '{LIN}/states.parquet' ORDER BY page_id, claim_id, start_ord""").fetchall():
        states[p].append(tuple(rest))
    orders = {(p, o): ids for p, o, ids in d.execute(
        f"SELECT page_id, ord, list(claim_id ORDER BY pos) FROM '{LIN}/claim_ids.parquet' GROUP BY 1, 2").fetchall()}
    cur = connect("wikihistory").cursor()
    cur.execute(f"SELECT page_id, rev_id, row_start, editor, anon, comment FROM page_state FOR SYSTEM_TIME ALL "
                f"WHERE page_id IN ({','.join(map(str, revs))})")
    ts: dict[tuple[int, int], datetime] = {}; meta: dict[tuple[int, int], tuple] = {}
    for p, r, start, editor, anon, comment in cur.fetchall():
        ts[(p, r)] = start; meta[(p, r)] = (editor, anon, comment)
    return revs, states, orders, ts, meta


def page_rows(p: int, revs: list, states: list, orders: dict, ts: dict, meta: dict) -> tuple[list, list, list, list]:
    """(claim history rows, current claim rows, layout history rows, current layout row) for one page."""
    times = [ts[(p, r)] for _, r, _ in revs]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise RuntimeError(f"page {p}: page_state times do not increase in the chunker's revision order")
    rev_at = {o: r for o, r, _ in revs}
    c_hist, c_cur = [], []
    for c, s_ord, e_ord, s_rev, th, eh, nr, sec in states:
        editor, anon, comment = meta[(p, s_rev)]
        row = (p, c, th, eh, sec, s_rev, editor, anon, nr, comment, fmt(ts[(p, s_rev)]))
        if e_ord is None:
            c_cur.append(row)
        else:
            c_hist.append(row + (fmt(ts[(p, rev_at[e_ord])]),))
    changes, prev = [], None
    for o, r, hidden in revs:
        if hidden:
            continue                                       # text unknown: claims and layout carry over
        order = orders.get((p, o), [])
        if order != prev:
            changes.append((r, json.dumps(order, separators=(",", ":")), ts[(p, r)])); prev = order
    l_rows = [(p, r, js, fmt(t)) for r, js, t in changes]
    l_hist = [row + (fmt(changes[i + 1][2]),) for i, row in enumerate(l_rows[:-1])]
    return c_hist, c_cur, l_hist, l_rows[-1:]


def load(schema: str, fresh: bool) -> dict:
    t0 = time.time()
    inputs = {p.name: digest(p) for p in INPUTS}            # what this run reads, taken before it reads anything
    revs, states, orders, ts, meta = source()              # validate the inputs before --fresh drops anything
    con = connect(schema); cur = con.cursor()
    if fresh:
        for t in TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    for t in TABLES:
        cur.execute(ddl(t).replace(f"CREATE TABLE {t} (", f"CREATE TABLE IF NOT EXISTS {t} (", 1))
    variants = duckdb.connect().execute(f"SELECT exact_hash, text_hash, text FROM '{CH}/chunk_variant.parquet' ORDER BY exact_hash").fetchall()
    cur.execute("SELECT COUNT(*) FROM text_variant"); have = cur.fetchone()[0]
    con.autocommit = False
    if have == 0:
        for i in range(0, len(variants), BATCH_ROWS):
            cur.executemany("INSERT INTO text_variant (exact_hash, text_hash, text) VALUES (?, ?, ?)", variants[i:i + BATCH_ROWS])
        con.commit()
    elif have != len(variants):
        raise SystemExit(f"text_variant holds {have:,} rows, expected {len(variants):,}: reload with --fresh")
    cur.execute("SELECT DISTINCT page_id FROM layout FOR SYSTEM_TIME ALL"); done = {p for (p,) in cur.fetchall()}
    cur.execute("SET system_versioning_insert_history = 1")
    batch: list[tuple] = []
    stats: dict[str, int] = dict(pages_loaded=0, pages_skipped=0, claim_rows=0, layout_rows=0)

    def flush() -> None:
        for c_hist, c_cur, l_hist, l_cur in batch:
            if c_hist:
                cur.executemany(f"INSERT INTO claim ({CLAIM_COLS}, row_end) VALUES ({', '.join('?' * 12)})", c_hist)
            if c_cur:
                cur.executemany(f"INSERT INTO claim ({CLAIM_COLS}) VALUES ({', '.join('?' * 11)})", c_cur)
            if l_hist:
                cur.executemany(f"INSERT INTO layout ({LAYOUT_COLS}, row_end) VALUES (?, ?, ?, ?, ?)", l_hist)
            if l_cur:                                      # a page whose revisions are all hidden has no layout
                cur.executemany(f"INSERT INTO layout ({LAYOUT_COLS}) VALUES (?, ?, ?, ?)", l_cur)
        con.commit(); batch.clear()

    for p in sorted(revs):
        if p in done:
            stats["pages_skipped"] += 1; continue
        rows = page_rows(p, revs[p], states.get(p, []), orders, ts, meta)
        batch.append(rows)
        stats["pages_loaded"] += 1; stats["claim_rows"] += len(rows[0]) + len(rows[1]); stats["layout_rows"] += len(rows[2]) + len(rows[3])
        if sum(len(r[0]) + len(r[1]) + len(r[2]) for r in batch) >= BATCH_ROWS:
            flush()
    flush(); con.close()
    return dict(schema=schema, fresh=fresh, **stats, text_variants=len(variants), seconds=round(time.time() - t0, 1),
                inputs=inputs, lineage_log=json.loads((ROOT / "logs/lineage.json").read_text(encoding="utf-8"))["resolver_sha256"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="wikihistory", choices=("wikihistory", "smoke"))
    ap.add_argument("--fresh", action="store_true", help="drop claim, layout and text_variant first")
    a = ap.parse_args()
    rec = load(a.schema, a.fresh)
    print(json.dumps({k: v for k, v in rec.items() if k != "inputs"}))
    log = ROOT / f"logs/load_claims_{a.schema}.json"
    runs = [] if a.fresh or not log.exists() else json.loads(log.read_text(encoding="utf-8")).get("runs", [])
    if rec["pages_loaded"] or a.fresh:                     # a no-op resume adds nothing to the record
        log.write_text(json.dumps(dict(runs=runs + [rec], inputs=rec["inputs"], lineage_log=rec["lineage_log"]), indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
