"""F6 building blocks: reconstruction through SQL AS OF, the expected lists from the chunker's output, and integrity
queries. Every function takes the schema, so fault injection (eval/f6_mutate.py) runs the same code on a copy in `smoke`.
The expected side never reads what the loader wrote: hash lists come from data/chunks/rev_chunks.parquet, the revision
active at a time from page_state (FOR SYSTEM_TIME AS OF / ALL in wikihistory)."""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path

import duckdb

from common import ROOT, connect

CH, LIN = ROOT / "data/chunks", ROOT / "data/lineage"
MUTATION_CODE_FILES = ("pipeline/load_claims.py", "eval/f6_lib.py", "eval/f6_check.py", "eval/f6_mutate.py")
ONE_US = timedelta(microseconds=1)
Rev = tuple[int, int, bool, datetime]           # (ord, rev_id, hidden, page_state row_start)


def ts_text(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S.%f")


def reconstruct(cur, schema: str, page_id: int, t: datetime) -> tuple[list[tuple[int, int]], set[int], set[int]]:
    """The plan's reconstruct() plus the exact spelling: [(text_hash, exact_hash)] in reading order at t (empty before the
    page's first visible revision), the claim ids in the layout, and the claim ids alive in `claim`."""
    s = ts_text(t)
    cur.execute(f"SELECT claim_order FROM {schema}.layout FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?", (s, page_id))
    row = cur.fetchone()
    order = json.loads(row[0]) if row else []
    cur.execute(f"SELECT claim_id, text_hash, exact_hash FROM {schema}.claim FOR SYSTEM_TIME AS OF TIMESTAMP ? WHERE page_id = ?", (s, page_id))
    alive = {c: (th, eh) for c, th, eh in cur.fetchall()}
    return [alive.get(c, (None, None)) for c in order], set(order), set(alive)


def revisions() -> dict[int, list[Rev]]:
    """Per page, every revision in page_state order with its bumped time (the source of truth for times)."""
    d = duckdb.connect()
    meta = {(p, r): (o, h) for p, r, o, h in d.execute(f"SELECT page_id, rev_id, ord, hidden FROM '{CH}/revisions.parquet'").fetchall()}
    cur = connect("wikihistory").cursor()
    cur.execute(f"SELECT page_id, rev_id, row_start FROM page_state FOR SYSTEM_TIME ALL WHERE page_id IN "
                f"({','.join(str(p) for p in {p for p, _ in meta})}) ORDER BY page_id, row_start")
    out: dict[int, list[Rev]] = defaultdict(list)
    for p, r, t in cur.fetchall():
        o, h = meta[(p, r)]
        out[p].append((o, r, h, t))
    return out


def expected_lists(revs: dict[int, list[Rev]]) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """(page, rev) -> [(text_hash, exact_hash)] from the chunker. A hidden revision has no text: it carries the previous
    visible revision's list (the lineage's rule), which is reported separately because it is not an independent source."""
    d = duckdb.connect()
    chunks: dict[tuple[int, int], list] = {}
    rows = d.execute(f"SELECT page_id, ord, text_hash, exact_hash FROM '{CH}/rev_chunks.parquet' ORDER BY page_id, ord, pos").fetchall()
    for (p, o), grp in groupby(rows, key=lambda x: (x[0], x[1])):
        chunks[(p, o)] = [(th, eh) for _, _, th, eh in grp]
    out = {}
    for p, rs in revs.items():
        last: list = []
        for o, r, hidden, _ in rs:
            if not hidden:
                last = chunks.get((p, o), [])
            out[(p, r)] = last
    return out


def chunk_count_audit() -> tuple[int, int]:
    """Compare every revisions.parquet n_chunks with the emitted rev_chunks rows."""
    d = duckdb.connect()
    total, mismatches = d.execute(f"""WITH emitted AS (
          SELECT page_id, ord, COUNT(*) AS n FROM '{CH}/rev_chunks.parquet' GROUP BY page_id, ord
        )
        SELECT COUNT(*), COUNT(*) FILTER (WHERE r.n_chunks <> COALESCE(e.n, 0))
        FROM '{CH}/revisions.parquet' r LEFT JOIN emitted e USING (page_id, ord)""").fetchone()
    return total, mismatches


def period_overlap_count(cur, source: str, partition: str) -> int:
    """Run the production period-overlap window query against a system-versioned table or fixture."""
    cur.execute(f"""SELECT COUNT(*) FROM (SELECT row_end, LEAD(row_start) OVER (PARTITION BY {partition}
                    ORDER BY row_start) nx FROM {source}) x WHERE nx < row_end""")
    return cur.fetchone()[0]


def integrity(schema: str) -> dict[str, int]:
    """Violations per kind, all in SQL over FOR SYSTEM_TIME ALL."""
    cur = connect(schema).cursor(); bad = {}
    q = lambda sql: (cur.execute(sql), cur.fetchone()[0])[1]
    bad["claim periods overlap"] = period_overlap_count(cur, f"{schema}.claim FOR SYSTEM_TIME ALL", "page_id, claim_id")
    bad["layout periods overlap"] = period_overlap_count(cur, f"{schema}.layout FOR SYSTEM_TIME ALL", "page_id")
    bad["layout gaps (a page's order must be continuous)"] = q(
        f"""SELECT COUNT(*) FROM (SELECT row_end, LEAD(row_start) OVER (PARTITION BY page_id ORDER BY row_start) nx
            FROM {schema}.layout FOR SYSTEM_TIME ALL) x WHERE nx IS NOT NULL AND nx <> row_end""")
    bad["pages without exactly one current layout row"] = q(
        f"SELECT COUNT(*) FROM (SELECT page_id FROM {schema}.layout FOR SYSTEM_TIME ALL GROUP BY page_id) a "
        f"WHERE (SELECT COUNT(*) FROM {schema}.layout l WHERE l.page_id = a.page_id) <> 1")
    for t in ("claim", "layout"):                      # the same bumped timestamps as page_state, same page
        for col in ("row_start", "row_end"):
            cond = "" if col == "row_start" else f"WHERE x.{col} < '2106-01-01'"
            bad[f"{t}.{col} not a page_state time of its page"] = q(
                f"""SELECT COUNT(*) FROM {schema}.{t} FOR SYSTEM_TIME ALL x {cond if cond else 'WHERE 1'} AND NOT EXISTS
                    (SELECT 1 FROM wikihistory.page_state FOR SYSTEM_TIME ALL s WHERE s.page_id = x.page_id AND s.row_start = x.{col})""")
    bad["claim rev_id or editor/anon/comment differs from page_state at row_start"] = q(
        f"""SELECT COUNT(*) FROM {schema}.claim FOR SYSTEM_TIME ALL x JOIN wikihistory.page_state FOR SYSTEM_TIME ALL s
            ON s.page_id = x.page_id AND s.row_start = x.row_start
            WHERE s.rev_id <> x.rev_id OR NOT (BINARY s.editor <=> BINARY x.editor) OR s.anon <> x.anon
               OR NOT (BINARY s.comment <=> BINARY x.comment)""")
    bad["layout row whose order equals the previous one (one row per change of order)"] = q(
        f"""SELECT COUNT(*) FROM (SELECT claim_order, LAG(claim_order) OVER (PARTITION BY page_id ORDER BY row_start) pv
            FROM {schema}.layout FOR SYSTEM_TIME ALL) x WHERE pv IS NOT NULL AND BINARY pv = BINARY claim_order""")
    bad["text_variant text that does not re-hash to its exact_hash and text_hash"] = variant_text_errors(schema)
    bad["layout rev_id differs from page_state at row_start"] = q(
        f"""SELECT COUNT(*) FROM {schema}.layout FOR SYSTEM_TIME ALL x JOIN wikihistory.page_state FOR SYSTEM_TIME ALL s
            ON s.page_id = x.page_id AND s.row_start = x.row_start WHERE s.rev_id <> x.rev_id""")
    bad["claim text_hash without a chunk row (the plan's F5 orphan query)"] = q(
        f"SELECT COUNT(*) FROM {schema}.claim FOR SYSTEM_TIME ALL c LEFT JOIN wikihistory.chunk k USING (text_hash) WHERE k.text_hash IS NULL")
    bad["claim exact_hash without a text_variant of the same text_hash"] = q(
        f"""SELECT COUNT(*) FROM {schema}.claim FOR SYSTEM_TIME ALL c LEFT JOIN {schema}.text_variant v ON v.exact_hash = c.exact_hash
            WHERE v.exact_hash IS NULL OR v.text_hash <> c.text_hash""")
    return bad


def variant_text_errors(schema: str) -> int:
    """Every exact spelling re-hashed: xxh64 of the text = exact_hash, the chunker's normalised hash = text_hash."""
    import sys
    import xxhash
    sys.path.insert(0, str(ROOT / "pipeline"))
    from chunker import text_hash
    cur = connect(schema).cursor(); cur.execute(f"SELECT exact_hash, text_hash, text FROM {schema}.text_variant")
    return sum(xxhash.xxh64_intdigest(t.encode("utf-8")) != e or text_hash(t) != h for e, h, t in cur.fetchall())


def rebuilt_claim_rows(revs: dict[int, list[Rev]]) -> list[tuple]:
    """The claim rows rebuilt independently from claim ids + chunks (not states.parquet, which the loader read): one row
    per run of a claim with the same (text, spelling, citations, section) over consecutive visible revisions, timed by
    page_state. (page_id, claim_id, text_hash, exact_hash, n_refs, section, rev_id, row_start, row_end or None)."""
    d = duckdb.connect()
    rows = d.execute(f"""SELECT i.page_id, i.ord, i.claim_id, r.text_hash, r.exact_hash, r.n_refs, r.section, i.rev_id
                         FROM '{LIN}/claim_ids.parquet' i JOIN '{CH}/rev_chunks.parquet' r USING (page_id, rev_id, ord, pos)
                         ORDER BY i.page_id, i.claim_id, i.ord""").fetchall()
    t_of = {(p, o): t for p, rs in revs.items() for o, _, _, t in rs}
    nxt = {}                                           # (page, visible ord) -> the next visible ord
    for p, rs in revs.items():
        vis = [o for o, _, h, _ in rs if not h]
        nxt.update({(p, a): b for a, b in zip(vis, vis[1:] + [None])})
    out = []
    for (p, c), grp in groupby(rows, key=lambda x: (x[0], x[2])):
        runs: list[list] = []
        for _, o, _, th, eh, nr, sec, rev in grp:
            key = (th, eh, nr, sec)
            if runs and runs[-1][2] == o and runs[-1][1] == key:
                runs[-1][2] = nxt[(p, o)]
            else:
                runs.append([rev, key, nxt[(p, o)], o])
        for rev, (th, eh, nr, sec), until, start in runs:
            out.append((p, c, th, eh, nr, sec, rev, t_of[(p, start)], None if until is None else t_of[(p, until)]))
    return out


def stored_claim_rows(schema: str) -> list[tuple]:
    cur = connect(schema).cursor()
    cur.execute(f"""SELECT page_id, claim_id, text_hash, exact_hash, n_refs, section, rev_id, row_start,
                           IF(row_end > '2106-01-01', NULL, row_end) FROM {schema}.claim FOR SYSTEM_TIME ALL""")
    return [tuple(r) for r in cur.fetchall()]


def checksum(schema: str) -> dict[str, object]:
    """Row counts and an order-independent sha256 over claim (page_id, claim_id, text_hash, exact_hash, row_start, row_end)
    and layout (page_id, rev_id, claim_order, row_start, row_end)."""
    import hashlib
    cur = connect(schema).cursor(); out: dict[str, object] = {}
    for t, cols, hist in (("claim", "page_id, claim_id, text_hash, exact_hash, row_start, row_end", "FOR SYSTEM_TIME ALL"),
                          ("layout", "page_id, rev_id, claim_order, row_start, row_end", "FOR SYSTEM_TIME ALL"),
                          ("text_variant", "exact_hash, text_hash, SHA2(text, 256)", "")):
        cur.execute(f"SELECT {cols} FROM {schema}.{t} {hist}")
        rows = sorted(repr(tuple(str(v) for v in r)) for r in cur.fetchall())
        out[f"{t}_rows"] = len(rows); out[f"{t}_sha256"] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    return out


def table_files_mb(schema: str) -> dict[str, float]:
    base = ROOT / ".mariadb/data" / schema
    return {t: round(sum(f.stat().st_size for f in base.glob(f"{t}.ibd")) / 2**20, 1) for t in ("claim", "layout", "text_variant", "chunk")}


def json_dump(x: object) -> str:
    return json.dumps(x, default=str)


PathT = Path
